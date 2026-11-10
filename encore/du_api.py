"""Design Uniformity: the shared part of the outside API (API-STANDARD.md, section 12).

Carried into each app by the design updater (like du_flask.py): don't edit it in an app; change it
here and release. It imports nothing from any app and talks to one only through `init_api(...)`
and the callables passed in. Lifted from Binge Stats, the standard's pilot (Nov 2026).

What it does, for every request under /api/v1/ and /display/:
- finds who's asking: `Authorization: Bearer`, else `?token=` (read tokens only), else an admin
  session (`is_admin_session`);
- refuses write tokens in the URL, bad tokens (a lockout after 10 in a minute from one address,
  keyed by CF-Connecting-IP through a tunnel), wrong scopes, too many requests per token, and
  tunnel requests when API_PUBLIC=0;
- adds a weak ETag to every successful GET and answers 304 to a matching If-None-Match;
- keeps the registry behind /api/v1/meta (feeds, actions, displays, event types).

And the pieces an app's routes use: `@standard(scope)` for the standard's routes, `@display` for
display pages, `image_response` (resize, cache, WebP or JPEG), `event_stream` and `StreamSlots`
for /api/v1/events, `sign` and `RETRY_AFTER_S` for webhooks, `iso` / `parse_iso` for times.

Draft 4 (v1.11), all optional: app tokens (`check_token` may return `app`) and `@standard("app")`
for /api/v1/inbound/, `add_inbound` and the inbound key and reopen rules, per-action limits
(`add_action(..., limit="10/hour")` and `core.over_limit(name)`), wall-clock times sent in
(`local_time`, `local_out`), and `webhook_matches` for the event-type and source filters.

The standard's routes get the standard's error shape, `{"error": "...", "code": "..."}`. An app
that already had /api/v1 routes keeps their error shape by passing `legacy_error`; they check
scope with `identity()`.

The rate limit and the lockout are in memory, per process. With several gunicorn workers they are
that many times looser; the standard accepts that (section 10).
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable

from flask import current_app, g, jsonify, request

API_VERSION = 1
CODES = ("bad_request", "unauthorised", "forbidden", "not_found", "rate_limited", "busy", "upstream", "error")
STATUS_CODE = {400: "bad_request", 401: "unauthorised", 403: "forbidden", 404: "not_found", 429: "rate_limited",
               503: "busy", 502: "upstream"}

BAD_TOKEN = "That token isn't valid. Make a new one in Settings → API tokens."
WRITE_IN_URL = "Write tokens go in the Authorization header, never the URL."
NO_TOKEN = "Send a token: Authorization: Bearer <token>, or ?token=<read token>."
READ_ONLY = "This token is read-only. Make a write token in Settings → API tokens."
LOCKED_OUT = "Too many bad tokens from this address. Try again later."
TOO_MANY = "Too many requests with this token. Slow down and try again shortly."
NOT_PUBLIC = "The API isn't available through the tunnel. Use the LAN address."
APP_ONLY = ("Only an app's own token can keep items here. Make one in Settings → API tokens and say which app "
            "it's for.")


def iso(ts: float | None) -> str | None:
    """UTC epoch seconds -> '2026-11-05T22:40:00Z' (the standard's time format)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> float | None:
    """'2026-11-05T22:40:00Z' (or with an offset) -> epoch seconds; None if it isn't a time."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass
class Identity:
    kind: str                 # "token" or "session"
    scope: str                # "read" or "write"
    token_id: int | None = None
    name: str = ""
    from_url: bool = False
    app: str | None = None    # set for an app token (API standard section 3, draft 4)


@dataclass
class Limits:
    bad_per_window: int = 10
    window_s: int = 60
    lockout_s: int = 15 * 60
    per_token_per_min: int = 120


class _Limiter:
    """Bad-token lockout per address and request rate per token, in memory, thread-safe."""

    def __init__(self, limits: Limits, clock: Callable[[], float]):
        self.limits, self.clock = limits, clock
        self._lock = threading.Lock()
        self._bad: dict[str, deque] = defaultdict(deque)
        self._locked: dict[str, float] = {}
        self._hits: dict[int, deque] = defaultdict(deque)

    def locked_for(self, addr: str) -> int:
        """Seconds left on this address's lockout, or 0."""
        with self._lock:
            until = self._locked.get(addr, 0)
            left = until - self.clock()
            if left <= 0:
                self._locked.pop(addr, None)
                return 0
            return int(left) + 1

    def bad_token(self, addr: str) -> int:
        """Count a bad token; returns the lockout in seconds if this one tipped it over, else 0."""
        now = self.clock()
        with self._lock:
            q = self._bad[addr]
            q.append(now)
            while q and q[0] <= now - self.limits.window_s:
                q.popleft()
            if len(q) >= self.limits.bad_per_window:
                q.clear()
                self._locked[addr] = now + self.limits.lockout_s
                return self.limits.lockout_s
            return 0

    def hit(self, token_id: int) -> int:
        """Count a request; returns seconds to wait if the token is over its limit, else 0."""
        now = self.clock()
        with self._lock:
            q = self._hits[token_id]
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= self.limits.per_token_per_min:
                return int(q[0] + 60 - now) + 1
            q.append(now)
            return 0

    def action_hit(self, token_id, action: str, n: int, per_s: int) -> int:
        """Count one use of a limited action by a token; seconds to wait if over `n` per `per_s`, else 0."""
        now = self.clock()
        with self._lock:
            q = self._hits.setdefault(("action", token_id, action), deque())  # type: ignore[arg-type]
            while q and q[0] <= now - per_s:
                q.popleft()
            if len(q) >= n:
                return int(q[0] + per_s - now) + 1
            q.append(now)
            return 0


_PER = {"minute": 60, "hour": 3600, "day": 86400}


def parse_limit(limit: str) -> tuple[int, int]:
    """'10/hour' -> (10, 3600). Raises ValueError for anything else."""
    try:
        n, per = limit.split("/")
        return int(n), _PER[per.strip()]
    except (ValueError, KeyError):
        raise ValueError(f"A limit looks like '10/hour' (minute, hour or day), not {limit!r}.") from None


@dataclass
class ApiCore:
    name: str
    title: str
    version: str
    sha: Callable[[], str]
    accent: str
    icon: str
    time_zone: Callable[[], str]
    check_token: Callable[[str], dict | None]
    is_admin_session: Callable[[], bool]
    csrf_ok: Callable[[], bool]
    legacy_error: Callable[[str, str, int], Any] | None = None
    public: bool = True
    limits: Limits = field(default_factory=Limits)
    clock: Callable[[], float] = time.time
    prefixes: tuple[str, ...] = ("/api/v1/", "/display/")
    open_endpoints: set[str] = field(default_factory=set)    # no token needed (e.g. a health check)
    unmetered_endpoints: set[str] = field(default_factory=set)  # images and events: not rate limited
    feeds: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    displays: list[dict] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    inbound: dict | None = None        # meta "inbound": {kind, fields}, for apps that keep other apps' items
    max_streams: int = 8               # per process; the standard's 8 per app, split across workers

    def __post_init__(self):
        self.limiter = _Limiter(self.limits, self.clock)
        self.stopping = threading.Event()
        self.streams = StreamSlots(self.max_streams)

    # -- registry, for /api/v1/meta --------------------------------------------------------------

    def add_feed(self, name: str, title: str, refresh_s: int, params: dict | None = None) -> None:
        self.feeds.append({"name": name, "title": title, "refresh_s": refresh_s, "params": params or {}})

    def add_action(self, name: str, title: str, body: dict, when: Callable[[], bool] | None = None,
                   limit: str | None = None) -> None:
        """`limit` ("10/hour") is on top of the token's own rate; enforce it with `over_limit(name)`."""
        a = {"name": name, "title": title, "body": body, "_when": when}
        if limit:
            parse_limit(limit)
            a["limit"] = limit
        self.actions.append(a)

    def over_limit(self, name: str):
        """For an action route: the 429 answer if this token is over the action's own limit, else None."""
        a = next((x for x in self.actions if x["name"] == name), None)
        who = identity()
        if not a or not a.get("limit") or who is None:
            return None
        n, per = parse_limit(a["limit"])
        wait = self.limiter.action_hit(who.token_id if who.kind == "token" else "session", name, n, per)
        if not wait:
            return None
        when = datetime.now(timezone.utc).timestamp() + wait
        local = _local_clock(when, self.time_zone())
        return self.error("rate_limited", f"That's {n} this {a['limit'].split('/')[1]} for {name}. "
                          f"The next one can go at {local}.", 429, wait)

    def add_inbound(self, kind: str, fields: dict) -> None:
        """This app keeps other apps' items (API standard section 7, draft 4): shown in meta as "inbound"."""
        self.inbound = {"kind": kind, "fields": fields}

    def add_display(self, name: str, title: str, sizes: list[str]) -> None:
        self.displays.append({"name": name, "title": title, "sizes": sizes})

    def add_events(self, types: list[str]) -> None:
        self.events.extend(t for t in types if t not in self.events)

    def meta(self) -> dict:
        return {
            "app": self.name, "name": self.title, "version": self.version, "sha": self.sha(), "api": API_VERSION,
            "accent": self.accent, "icon": self.icon, "time_zone": self.time_zone(),
            "feeds": list(self.feeds),
            "actions": [{k: v for k, v in a.items() if k != "_when"} for a in self.actions
                        if a["_when"] is None or a["_when"]()],
            "displays": list(self.displays),
            "events": list(self.events),
            **({"inbound": self.inbound} if self.inbound else {}),
        }

    # -- answers -----------------------------------------------------------------------------------

    @staticmethod
    def error(code: str, message: str, status: int, retry_after: int | None = None):
        resp = jsonify({"error": message, "code": code})
        resp.status_code = status
        resp.headers["Cache-Control"] = "no-store"   # a shared cache keeps a 404 otherwise (APP-STANDARD 12)
        if retry_after is not None or status in (429, 503):
            resp.headers["Retry-After"] = str(retry_after if retry_after is not None else 30)
        return resp

    def _refuse(self, code: str, message: str, status: int, retry_after: int | None = None):
        if _is_display():
            resp = display_error(message, status)
            if retry_after is not None:
                resp.headers["Retry-After"] = str(retry_after)
            return resp
        if _is_standard() or self.legacy_error is None:
            return self.error(code, message, status, retry_after)
        resp = current_app.make_response(self.legacy_error(code, message, status))
        if retry_after is not None:
            resp.headers["Retry-After"] = str(retry_after)
        return resp

    # -- the request gate ----------------------------------------------------------------------------

    def _applies(self) -> bool:
        return request.path.startswith(self.prefixes)

    def before(self):
        if not self._applies():
            return None
        g.api_identity = None
        if not self.public and request.headers.get("CF-Connecting-IP"):
            return self._refuse("forbidden", NOT_PUBLIC, 403)
        if request.endpoint in self.open_endpoints:
            return None
        addr = client_address()
        left = self.limiter.locked_for(addr)
        if left:
            return self._refuse("rate_limited", LOCKED_OUT, 429, left)

        token, from_url = None, False
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
        elif request.args.get("token"):
            token, from_url = request.args["token"].strip(), True
        if token:
            row = self.check_token(token)
            if row is None:
                locked = self.limiter.bad_token(addr)
                if locked:
                    return self._refuse("rate_limited", LOCKED_OUT, 429, locked)
                return self._refuse("unauthorised", BAD_TOKEN, 401)
            if from_url and row["scope"] == "write":
                return self._refuse("bad_request", WRITE_IN_URL, 400)
            if request.endpoint not in self.unmetered_endpoints:
                wait = self.limiter.hit(row["id"])
                if wait:
                    return self._refuse("rate_limited", TOO_MANY, 429, wait)
            g.api_identity = Identity("token", row["scope"], row["id"], row.get("name", ""), from_url,
                                      row.get("app") or None)
        elif self.is_admin_session():
            g.api_identity = Identity("session", "write", None, "Admin session")
        return None

    def after(self, resp):
        if (self._applies() and request.method == "GET" and resp.status_code == 200
                and not resp.is_streamed and not resp.direct_passthrough and resp.headers.get("ETag") is None):
            resp.add_etag(weak=True)
            resp.make_conditional(request)
        return resp


def client_address() -> str:
    """Who to lock out. Through a Cloudflare tunnel every request arrives from cloudflared's address,
    so one stranger's bad tokens would lock out every tunnel reader; Cloudflare sets
    CF-Connecting-IP to the real client and overwrites any copy sent from outside, so use it when
    it's there. (Faking it on the LAN only dodges the lockout, and tokens are 256-bit.)"""
    return (request.headers.get("CF-Connecting-IP") or "").strip() or request.remote_addr or "?"


def core() -> ApiCore:
    return current_app.extensions["du_api"]


def identity() -> Identity | None:
    return g.get("api_identity")


def _is_display() -> bool:
    fn = current_app.view_functions.get(request.endpoint or "")
    return bool(getattr(fn, "_du_display", False))


def _is_standard() -> bool:
    fn = current_app.view_functions.get(request.endpoint or "")
    return bool(getattr(fn, "_du_standard", False))


def standard(scope: str = "read"):
    """Mark a route as one of the standard's and check scope; refusals use the standard's shape.
    "app" is for /api/v1/inbound/: an app token only (a write token that belongs to an app)."""
    if scope not in ("read", "write", "app"):
        raise ValueError(scope)

    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            who = identity()
            if who is None:
                return ApiCore.error("unauthorised", NO_TOKEN, 401)
            if scope == "app" and (who.kind != "token" or who.scope != "write" or not who.app):
                return ApiCore.error("forbidden", APP_ONLY, 403)
            if scope == "write":
                if who.scope != "write":
                    return ApiCore.error("forbidden", READ_ONLY, 403)
                if who.kind == "session" and not core().csrf_ok():
                    return ApiCore.error("bad_request", "Your session expired. Reload the page and try again.", 400)
            return fn(*a, **kw)
        wrapper._du_standard = True  # type: ignore[attr-defined]
        return wrapper
    return deco


# -- times sent in, inbound items, webhook filters (API standard draft 4) ---------------------------

import re as _re

INBOUND_KEY = _re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
LOCAL_FORMS = "local time as 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' for all day"


def _local_clock(ts: float, tz: str) -> str:
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%H:%M")


def valid_key(key: str) -> bool:
    """An inbound item's key: the sender's own stable id, [A-Za-z0-9._:-], 1 to 100 characters."""
    return bool(INBOUND_KEY.match(key or ""))


def local_time(s: str, field_name: str = "due") -> tuple[str, str | None]:
    """A wall-clock time sent in ('2026-12-24 09:00' or '2026-12-24') -> ('2026-12-24', '09:00' | None).
    Raises ValueError with a sentence for anything else, including a UTC 'Z' time."""
    from datetime import date as _date, time as _time
    text = (s or "").strip()
    m = _re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}))?", text)
    if not m:
        raise ValueError(f"{field_name} is a {LOCAL_FORMS}, not {s!r}.")
    try:
        _date.fromisoformat(m.group(1))
        if m.group(2):
            _time.fromisoformat(m.group(2))
    except ValueError:
        raise ValueError(f"{field_name} isn't a real date and time: {s!r}.") from None
    return m.group(1), m.group(2)


def local_out(day: str | None, at: str | None, tz: str, all_day_time: str = "09:00") -> dict:
    """A wall-clock date and time -> {"due": "2026-12-24 09:00", "due_at": "...Z"} for an answer. An all-day
    date's due_at is the moment the app treats as its time (its all-day reminder time)."""
    if not day:
        return {"due": None, "due_at": None}
    from zoneinfo import ZoneInfo
    local = datetime.fromisoformat(f"{day} {at or all_day_time}").replace(tzinfo=ZoneInfo(tz))
    return {"due": f"{day} {at}" if at else day, "due_at": iso(local.timestamp())}


def reopens(status: str, old_due: tuple[str | None, str | None], new_due: tuple[str | None, str | None]) -> bool:
    """A PUT on a done inbound item reopens it only for a later due date: that's the next cycle."""
    if status != "done" or not new_due[0]:
        return False
    if not old_due[0]:
        return True
    return (new_due[0], new_due[1] or "00:00") > (old_due[0], old_due[1] or "00:00")


def webhook_matches(event_types: list[str] | None, source_app: str | None, event: dict) -> bool:
    """Should this event go to a webhook? Its event types ([] or None means all) and, if it's set to
    "Only items from" one app, only events about that app's inbound items."""
    if event_types and event.get("type") not in event_types:
        return False
    if source_app:
        return ((event.get("source") or {}).get("app") or "") == source_app
    return True


# -- images (API standard section 6) ---------------------------------------------------------------

IMAGE_WIDTHS = (80, 160, 320, 640, 1280)
IMAGE_MAX_AGE = 86400
IMAGE_CACHE_BYTES = 500 * 1024 * 1024


def snap(n: int) -> int:
    return next((w for w in IMAGE_WIDTHS if w >= n), IMAGE_WIDTHS[-1])


@dataclass(frozen=True)
class ImageAsk:
    """What a reader asked for, after snapping and clamping."""
    width: int | None
    height: int | None
    fmt: str               # webp | jpeg
    quality: int
    gray: bool

    @property
    def box(self) -> int:
        """The size to fetch the original at: the snapped width (or height)."""
        return self.width or self.height or 320

    def key(self, kind: str, ident: str) -> str:
        import hashlib
        raw = f"{kind}|{ident}|{self.width}|{self.height}|{self.fmt}|{self.quality}|{int(self.gray)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:40]


def image_ask(args, accept: str) -> ImageAsk:
    def num(name):
        try:
            return int(args.get(name) or 0) or None
        except ValueError:
            return None
    w, h = num("w"), num("h")
    w = snap(w) if w else None
    h = snap(h) if h and not w else None
    if not w and not h:
        w = 320
    fmt = (args.get("fmt") or "").lower()
    if fmt not in ("webp", "jpeg"):
        fmt = "webp" if "image/webp" in (accept or "") else "jpeg"
    q = num("q") or 80
    return ImageAsk(w, h, fmt, max(40, min(95, q)), args.get("gray") in ("1", "true"))


def render_image(original: bytes, ask: ImageAsk) -> bytes:
    """Resize (never up), optionally greyscale, and encode."""
    import io

    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(original)) as im:
        im = ImageOps.exif_transpose(im)
        if ask.gray:
            im = im.convert("L")
        elif im.mode not in ("RGB", "L"):
            im = im.convert("RGBA" if ask.fmt == "webp" and "A" in im.getbands() else "RGB")
        if ask.width and im.width > ask.width:
            im = im.resize((ask.width, max(1, round(im.height * ask.width / im.width))), Image.LANCZOS)
        elif ask.height and im.height > ask.height:
            im = im.resize((max(1, round(im.width * ask.height / im.height)), ask.height), Image.LANCZOS)
        if ask.fmt == "jpeg" and im.mode == "RGBA":
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, "WEBP" if ask.fmt == "webp" else "JPEG", quality=ask.quality,
                **({"optimize": True, "progressive": True} if ask.fmt == "jpeg" else {"method": 4}))
        return out.getvalue()


def image_response(cache_dir, kind: str, ident: str, fetch: Callable[[int], bytes | None]):
    """GET /api/v1/images/<kind>/<id>: from the disk cache, else `fetch(box)` (the original, at
    least `box` wide, fetched with the app's own credentials) resized here. 404 if there's none."""
    from pathlib import Path

    from flask import send_file
    ask = image_ask(request.args, request.headers.get("Accept", ""))
    d = Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    key = ask.key(kind, ident)
    path = d / f"{key}.{'webp' if ask.fmt == 'webp' else 'jpg'}"
    if not path.exists():
        try:
            original = fetch(ask.box)
        except Exception:  # noqa: BLE001 - any failure upstream is just "no image"
            original = None
        if not original:
            return ApiCore.error("not_found", "There's no image for that.", 404)
        try:
            data = render_image(original, ask)
        except Exception:  # noqa: BLE001 - not an image we can read
            return ApiCore.error("not_found", "There's no image for that.", 404)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    resp = send_file(path, mimetype=f"image/{ask.fmt}", conditional=True, etag=key, max_age=IMAGE_MAX_AGE)
    # private: an image needs a token like the rest of the API, so no shared cache (the tunnel's Cloudflare)
    # may keep one and hand it to a request without a token. The reader's own browser still keeps it a day.
    resp.headers["Cache-Control"] = f"private, max-age={IMAGE_MAX_AGE}"
    resp.headers["Vary"] = "Accept"
    return resp


def prune_image_cache(cache_dir, max_bytes: int = IMAGE_CACHE_BYTES) -> int:
    """Delete the oldest files (by mtime) until the cache is under max_bytes. Returns how many went.
    Run from a background job, never in a request."""
    from pathlib import Path
    d = Path(cache_dir)
    if not d.is_dir():
        return 0
    files = []
    for p in d.iterdir():
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file():
            files.append((st.st_mtime, st.st_size, p))
    total = sum(f[1] for f in files)
    gone = 0
    for _mtime, size, p in sorted(files):
        if total <= max_bytes:
            break
        try:
            p.unlink()
        except OSError:
            continue
        total -= size
        gone += 1
    return gone


# -- the events stream (API standard section 8) ------------------------------------------------------

STREAM_POLL_S = 1.0
STREAM_PING_S = 25.0
STREAM_MAX_S = 3600.0      # then the reader reconnects with Last-Event-ID; nothing is lost
STREAM_RETRY_MS = 5000


def sse(event: dict) -> str:
    import json
    return f"event: {event['type']}\nid: {event['id']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


def event_stream(rows_after: Callable[[int], list[dict]], last_id: int, *, stopping: threading.Event,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Any] | None = None,
                 poll_s: float = STREAM_POLL_S, ping_s: float = STREAM_PING_S, max_s: float = STREAM_MAX_S):
    """Yield SSE text: what's after `last_id` straight away, then new rows as they appear, checked
    every `poll_s`, with a `: ping` every `ping_s` so proxies (Cloudflare closes idle ones at
    about 100 s) keep it open. Ends after `max_s`, or as soon as `stopping` is set (SIGTERM)."""
    wait = sleep or stopping.wait
    started = last_ping = clock()
    yield f"retry: {STREAM_RETRY_MS}\n\n"
    while not stopping.is_set():
        for ev in rows_after(last_id):
            last_id = ev["id"]
            yield sse(ev)
        now = clock()
        if now - started >= max_s:
            return
        if now - last_ping >= ping_s:
            last_ping = now
            yield ": ping\n\n"
        wait(poll_s)


class StreamSlots:
    """At most `n` open streams in this process; each holds a gunicorn thread."""

    def __init__(self, n: int):
        self.n = n
        self._sem = threading.BoundedSemaphore(n) if n > 0 else None

    def take(self) -> Callable[[], None] | None:
        if self._sem is None or not self._sem.acquire(blocking=False):
            return None
        done = threading.Event()

        def release():
            if not done.is_set():
                done.set()
                self._sem.release()
        return release


def stop_streams_on_sigterm(c: "ApiCore") -> None:
    """Chain onto the process's SIGTERM handler (gunicorn's worker sets one before loading the app),
    so open streams end at once and a restart doesn't wait out the graceful timeout."""
    import signal
    if threading.current_thread() is not threading.main_thread():
        return
    previous = signal.getsignal(signal.SIGTERM)

    def handler(signum, frame):
        c.stopping.set()
        if callable(previous):
            previous(signum, frame)
        elif previous == signal.SIG_DFL:
            raise SystemExit(0)
    signal.signal(signal.SIGTERM, handler)


# -- display pages (API standard section 9) ----------------------------------------------------------

DISPLAY_NEEDS_TOKEN = "This display needs a read token: add ?token=… to the address (Settings → API tokens)."


def display_headers(resp):
    """A display page carries its token in its own URLs, so it's never cached or sent on as a referrer."""
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def display_error(message: str, status: int):
    """A plain page, not JSON: whatever shows a display page is a screen with nobody to read JSON."""
    from html import escape

    from flask import make_response
    body = ("<!doctype html><html lang=\"en-GB\"><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width, initial-scale=1\"><title>Display</title>"
            "<body style=\"margin:0;min-height:100vh;display:grid;place-items:center;background:#090b11;"
            "color:#9aa1b2;font:16px/1.5 system-ui,sans-serif;text-align:center;padding:24px\">"
            f"<p>{escape(message, quote=False)}</p></body></html>")
    return display_headers(make_response(body, status, {"Content-Type": "text/html; charset=utf-8"}))


def display(fn):
    """Mark a route as a display page (read access; refusals as a plain page, not JSON)."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if identity() is None:
            return display_error(DISPLAY_NEEDS_TOKEN, 401)
        return display_headers(current_app.make_response(fn(*a, **kw)))
    wrapper._du_display = True  # type: ignore[attr-defined]
    return wrapper


# -- webhooks (API standard section 8) ---------------------------------------------------------------

RETRY_AFTER_S = (60, 180, 360)     # after the first try: 3 retries over 10 minutes, then give up
WEBHOOK_TIMEOUT_S = 10


def sign(body: bytes, secret: str) -> str:
    """The X-Signature header for a webhook body: sha256=<hex HMAC-SHA256 of the raw body>."""
    import hashlib
    import hmac
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, secret: str, header: str) -> bool:
    """For a receiver: does X-Signature match this body and secret?"""
    import hmac
    return hmac.compare_digest(sign(body, secret), header or "")


def cached(resp, refresh_s: int):
    """Cache-Control for status and feeds: private, half the refresh interval."""
    resp.headers["Cache-Control"] = f"private, max-age={max(0, refresh_s // 2)}"
    return resp


def init_api(app, *, name: str, title: str, version: str, sha: Callable[[], str], accent: str, icon: str,
             time_zone: Callable[[], str], check_token: Callable[[str], dict | None],
             is_admin_session: Callable[[], bool], csrf_ok: Callable[[], bool],
             legacy_error: Callable[[str, str, int], Any] | None = None, limits: Limits | None = None,
             clock: Callable[[], float] | None = None, public: bool | None = None,
             max_streams: int = 8) -> ApiCore:
    """Install the gate on `app` and return the core (also at app.extensions["du_api"]).

    `public` defaults to the API_PUBLIC env var (1 unless it's "0"). Register the standard's
    routes on the app's own blueprint, marked with @standard; add `meta` with `core().meta()`.
    """
    if public is None:
        public = os.environ.get("API_PUBLIC", "1").strip() != "0"
    c = ApiCore(name=name, title=title, version=version, sha=sha, accent=accent, icon=icon, time_zone=time_zone,
                check_token=check_token, is_admin_session=is_admin_session, csrf_ok=csrf_ok,
                legacy_error=legacy_error, public=public, limits=limits or Limits(), clock=clock or time.time,
                max_streams=max_streams)
    app.extensions["du_api"] = c
    app.before_request(c.before)
    app.after_request(c.after)
    return c
