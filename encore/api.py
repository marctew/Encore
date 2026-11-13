"""Encore's outside API: /api/v1/ and /display/, to API-STANDARD.md (docs/design), documented in docs/API.md.

The shared part (the token gate, ETags, images, the events stream, signing) is du_api.py, carried in by
the design updater. This module is what's Encore's own: the status, the feeds, the actions, the events
the queue publishes, webhook delivery, the downloads display page, and the Settings forms for tokens and
webhooks. web.create_app() calls install(); the page's own /api/ routes are untouched.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import requests
from flask import Blueprint, Response, redirect, render_template, request, url_for

from . import __version__, engine, match
from .config import DATA, PHOTOS
from .du_api import (RETRY_AFTER_S, WEBHOOK_TIMEOUT_S, ApiCore, cached, core, display, event_stream, identity,
                     image_response, init_api, iso, parse_iso, prune_image_cache, sign, standard,
                     stop_streams_on_sigterm)
from .sources import SourceError, cover

log = logging.getLogger("encore")
bp = Blueprint("encore_api", __name__)

TIME_ZONE = os.environ.get("ENCORE_TIME_ZONE", "Europe/London")
CACHE = Path(os.environ.get("ENCORE_CACHE", "/var/cache/encore"))
TOKEN_PREFIX = "enc_"
STATUS_REFRESH = 30
FEED_REFRESH = {"downloads": 15, "arrivals": 300, "failed": 60, "wanted": 600}
EVENT_TYPES = ["job.queued", "download.started", "download.finished", "download.failed", "wanted.added"]
DISPLAY_SIZES = ["800x480", "1280x720", "1920x1080"]
RUNNING = ("downloading", "verifying", "filing", "scanning")
STATE_TEXT = {"queued": "Waiting", "downloading": "Downloading", "verifying": "Checking every track",
              "filing": "Filing", "scanning": "Plex is scanning", "done": "Done", "failed": "Failed",
              "review": "Waiting for you to check where each track starts"}
KEEP_EVENTS = 200
WANTED_PAUSE = 2


class Refused(Exception):
    """An action that can't run, with the standard's code, a sentence and a status."""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def svc():
    from .web import svc as web_svc
    return web_svc()


# ---------------------------------------------------------------- tokens and CSRF

def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def check_token(token):
    s = svc()
    row = s.store.one("SELECT id, name, scope, last_used_at FROM api_tokens WHERE hash = ? AND revoked_at IS NULL",
                      token_hash(token))
    if not row:
        return None
    if time.time() - (row["last_used_at"] or 0) > 60:
        s.store.db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (time.time(), row["id"]))
    return {"id": row["id"], "name": row["name"], "scope": row["scope"]}


def csrf_secret():
    path = DATA / "csrf.key"
    if not path.exists():
        path.touch(mode=0o600)
        path.write_text(secrets.token_hex(32))
        os.chmod(path, 0o600)
    return path.read_text().strip().encode()


def csrf_value():
    """The page's CSRF token: tied to the session cookie, so it changes with each sign-in."""
    from .web import COOKIE
    cookie = request.cookies.get(COOKIE) or "anonymous"
    return hmac.new(csrf_secret(), cookie.encode(), hashlib.sha256).hexdigest()[:32]


def csrf_ok():
    """A session's write: the page's own header, or the token as a header or a form field."""
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    return request.headers.get("X-Encore") == "1" or hmac.compare_digest(sent, csrf_value())


def is_admin_session():
    from .web import COOKIE
    auth = svc().auth
    return auth.enabled and auth.valid(request.cookies.get(COOKIE))


# ---------------------------------------------------------------- times and shapes

def at_label(ts):
    """The moment written for a person, in Encore's time zone: Today 19:12, Yesterday 08:01, Sat 7 Nov."""
    if not ts:
        return None
    zone = ZoneInfo(TIME_ZONE)
    when, today = datetime.fromtimestamp(ts, zone), datetime.now(zone).date()
    if when.date() == today:
        return f"Today {when:%H:%M}"
    if when.date() == today - timedelta(days=1):
        return f"Yesterday {when:%H:%M}"
    if abs((when.date() - today).days) < 7:
        return f"{when:%a %H:%M}"
    return f"{when:%a} {when.day} {when:%b}"


def base_url():
    return request.host_url.rstrip("/")


def job_text(job):
    if job["state"] == "downloading":
        step = job.get("step") or ""
        if job.get("kind") == "youtube" or step.startswith("YouTube"):
            return f"{step or 'Downloading'} · {round(job.get('percent') or 0)}%"
        return f"SABnzbd: {step}" if step else f"Downloading {round(job.get('percent') or 0)}%"
    if job["state"] == "filing" and job.get("step"):
        return f"Filing: {job['step'].lower()} · {round(job.get('percent') or 0)}%"
    if job["state"] in ("done", "failed"):
        return job.get("message") or STATE_TEXT[job["state"]]
    return STATE_TEXT.get(job["state"], job["state"])


def image_for(job):
    if job.get("kind") in ("video", "single"):
        return f"/api/v1/images/artist/{job['artist_id']}" if job.get("artist_id") else None
    return f"/api/v1/images/cover/{job['album_id']}"


def job_item(job):
    tone = {"failed": "danger", "done": "success", "queued": "muted", "review": "warning"}.get(job["state"], "info")
    return {"id": f"job:{job['id']}", "title": f"{job['artist']} – {job['title']}", "subtitle": job_text(job),
            "at": iso(job.get("added")), "at_label": at_label(job.get("added")), "tone": tone, "image": image_for(job),
            "url": f"{base_url()}/queue",
            "meta": {"job_id": job["id"], "album_id": job["album_id"], "artist": job["artist"], "album": job["title"],
                     "kind": job.get("kind") or "album", "state": job["state"], "percent": round(job.get("percent") or 0),
                     "quality": job.get("quality") or None}}


def jobs_now():
    queue = svc().queue
    with queue.lock:
        return [dict(j) for j in queue.jobs]


# ---------------------------------------------------------------- status

def status_body():
    s = svc()
    jobs = jobs_now()
    running = [j for j in jobs if j["state"] in RUNNING]
    waiting = [j for j in jobs if j["state"] == "queued"]
    failed = [j for j in jobs if j["state"] == "failed"]
    review = [j for j in jobs if j["state"] == "review"]
    midnight = datetime.now(ZoneInfo(TIME_ZONE)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    arrived = s.store.one("SELECT COUNT(*) AS n FROM arrivals WHERE at >= ?", midnight)["n"]
    detail = None
    if not engine.folders_ready():
        state, headline = "error", "Encore's folders aren't set, so nothing can be filed"
    elif running:
        lead = running[0]
        busy = [j for j in running if j["state"] == "downloading"]
        state = "busy"
        headline = (f"{len(busy)} album{'' if len(busy) == 1 else 's'} downloading" if busy
                    else f"{STATE_TEXT[lead['state']]}: {lead['artist']} – {lead['title']}")
        detail = f"Now: {lead['artist']} – {lead['title']}, {job_text(lead)}"
    elif failed:
        state, headline = "warning", f"{len(failed)} download{'' if len(failed) == 1 else 's'} failed"
        detail = f"{failed[-1]['artist']} – {failed[-1]['title']}: {failed[-1].get('message') or 'failed'}"
    elif review:
        state, headline = "warning", f"{len(review)} album{'' if len(review) == 1 else 's'} to check"
        detail = f"{review[0]['artist']} – {review[0]['title']}: check where each track starts, then file it"
    elif not s.sab.ready:
        state, headline = "warning", "SABnzbd isn't set up"
    else:
        state, headline = "ok", "Nothing downloading"
    if waiting and not detail:
        detail = f"Next: {waiting[0]['artist']} – {waiting[0]['title']}"
    return {"app": "encore", "state": state, "headline": headline, "detail": detail,
            "counts": [{"key": "downloading", "label": "Downloading", "value": len(running)},
                       {"key": "queued", "label": "Queued", "value": len(waiting)},
                       {"key": "failed", "label": "Failed", "value": len(failed), **({"tone": "danger"} if failed else {})},
                       {"key": "to_check", "label": "To check", "value": len(review), **({"tone": "warning"} if review else {})},
                       {"key": "arrived_today", "label": "Arrived today", "value": arrived},
                       {"key": "wanted", "label": "Wanted", "value": len(s.store.wanted_ids())}],
            "url": f"{base_url()}/", "generated_at": iso(time.time()), "refresh_s": STATUS_REFRESH}


@bp.get("/api/v1/meta")
@standard("read")
def meta():
    return cached(_json(core().meta()), STATUS_REFRESH)


@bp.get("/api/v1/status")
@standard("read")
def status():
    return cached(_json(status_body()), STATUS_REFRESH)


def _json(body, status_code=200):
    return Response(json.dumps(body, ensure_ascii=False, separators=(",", ":")), status_code, mimetype="application/json")


# ---------------------------------------------------------------- feeds

def limit_arg():
    try:
        return max(1, min(100, int(request.args.get("limit") or 20)))
    except ValueError:
        raise Refused("bad_request", "limit is a number from 1 to 100.") from None


def time_window():
    """since and cursor, for the feeds ordered by time, newest first."""
    since = parse_iso(request.args.get("since")) if request.args.get("since") else None
    if request.args.get("since") and since is None:
        raise Refused("bad_request", "since is an ISO time, like 2026-11-05T22:40:00Z.")
    cursor = request.args.get("cursor")
    try:
        before = float(cursor) if cursor else None
    except ValueError:
        raise Refused("bad_request", "That cursor isn't one this feed gave out.") from None
    return since, before


def page_of(rows, limit, at_of):
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (repr(at_of(rows[-1])) if more and rows else None)


def feed_downloads(limit):
    jobs = jobs_now()
    ordered = ([j for j in jobs if j["state"] in RUNNING] + [j for j in jobs if j["state"] == "review"]
               + [j for j in jobs if j["state"] == "queued"])
    return [job_item(j) for j in ordered[:limit]], None


def feed_failed(limit):
    failed = [j for j in jobs_now() if j["state"] == "failed"][::-1]
    return [job_item(j) for j in failed[:limit]], None


def arrival_item(row):
    data = json.loads(row["data"])
    where = data.get("where")
    subtitle = ("Music video" if data.get("kind") == "video" else "Single" if data.get("kind") == "single" and where != "import" else
                "In MusicImport" if where == "import" else "In your library")
    if data.get("quality"):
        subtitle += f" · {data['quality']}"
    return {"id": f"arrival:{row['job_id']}", "title": f"{data['artist']} – {data['title']}", "subtitle": subtitle,
            "at": iso(row["at"]), "at_label": at_label(row["at"]), "tone": "warning" if where == "import" else "success",
            "image": image_for(data), "url": f"{base_url()}/queue",
            "meta": {"job_id": row["job_id"], "album_id": data["album_id"], "artist": data["artist"], "album": data["title"],
                     "kind": data.get("kind") or "album", "where": where, "folder": data.get("folder"),
                     "quality": data.get("quality"), "message": data.get("message")}}


def feed_arrivals(limit):
    since, before = time_window()
    sql, args = "SELECT * FROM arrivals WHERE 1=1", []
    if since is not None:                  # times go out in whole seconds, so "after" means from the next one
        sql, args = sql + " AND at >= ?", [*args, since + 1]
    if before is not None:
        sql, args = sql + " AND at < ?", [*args, before]
    rows = svc().store.rows(sql + " ORDER BY at DESC LIMIT ?", *args, limit + 1)
    rows, cursor = page_of(rows, limit, lambda r: r["at"])
    return [arrival_item(r) for r in rows], cursor


def feed_wanted(limit):
    since, before = time_window()
    albums = [a for a in svc().store.wanted() if (since is None or a["added"] >= since + 1) and (before is None or a["added"] < before)]
    albums, cursor = page_of(albums, limit, lambda a: a["added"])
    return [{"id": f"wanted:{a['id']}", "title": f"{a['artist']} – {a['title']}",
             "subtitle": " · ".join(x for x in (a.get("year"), a.get("kind")) if x), "at": iso(a["added"]),
             "at_label": at_label(a["added"]), "tone": "accent", "image": f"/api/v1/images/cover/{a['id']}",
             "url": f"{base_url()}/wanted",
             "meta": {"album_id": a["id"], "artist_id": a.get("artist_id") or None, "artist": a["artist"],
                      "album": a["title"], "year": a.get("year") or None}} for a in albums], cursor


FEEDS = {
    "downloads": ("Downloading and up next", feed_downloads, {"limit": "int"}),
    "arrivals": ("Just arrived", feed_arrivals, {"limit": "int", "since": "time", "cursor": "str"}),
    "failed": ("Failed", feed_failed, {"limit": "int"}),
    "wanted": ("Wanted", feed_wanted, {"limit": "int", "since": "time", "cursor": "str"}),
}


@bp.get("/api/v1/feeds/<name>")
@standard("read")
def feed(name):
    if name not in FEEDS:
        return ApiCore.error("not_found", f"There's no feed called '{name}'. See /api/v1/meta for the feeds.", 404)
    title, make, _ = FEEDS[name]
    try:
        items, cursor = make(limit_arg())
    except Refused as refused:
        return ApiCore.error(refused.code, refused.message, refused.status)
    body = {"feed": name, "title": title, "items": items, "next_cursor": cursor, "generated_at": iso(time.time()),
            "refresh_s": FEED_REFRESH[name]}
    return cached(_json(body), FEED_REFRESH[name])


# ---------------------------------------------------------------- images

def image_cache():
    folder = CACHE / "images"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if os.access(folder, os.W_OK):
            return folder
    except OSError:
        pass
    return DATA / "image-cache"          # /var/cache/encore isn't set up (scripts/update.sh makes it)


@bp.get("/api/v1/images/<kind>/<ident>")
@standard("read")
def image(kind, ident):
    from .web import ALBUM_ID
    s = svc()
    if kind not in ("cover", "artist") or not ALBUM_ID.match(ident):
        return ApiCore.error("not_found", "There's no image for that.", 404)

    def fetch(box):
        if kind == "cover":
            response = s.brainz.web.get(cover(ident, 500 if box <= 500 else 1200))
            return response.content if response.status_code == 200 else None
        row = s.store.plex_artist(ident)
        if row and row["thumb"]:
            return s.plex.image(row["thumb"])[0]
        path = PHOTOS / f"{ident}.jpg"
        return path.read_bytes() if path.exists() else None

    return image_response(image_cache(), kind, ident, fetch)


# ---------------------------------------------------------------- actions

def album_id_from(body):
    from .web import ALBUM_ID
    album_id = str(body.get("album_id") or "")
    if not ALBUM_ID.match(album_id):
        raise Refused("bad_request", "album_id is a MusicBrainz release group id, like 1b022e01-4da6-387b-8658-8678046e4cef.")
    return album_id


def details_of(album_id):
    s = svc()
    details = s.brainz.album(album_id)
    s.store.save_album({k: details[k] for k in ("id", "title", "artist", "artist_id", "year", "kind", "cover")})
    return details


def active_job(album_id):
    queue = svc().queue
    with queue.lock:
        return next((j for j in queue.jobs if j["album_id"] == album_id and j["state"] in engine.ACTIVE), None)


def owned(details):
    from .web import mark_owned
    copy = dict(details)
    mark_owned([copy])
    return copy["owned"] if copy["owned"] and copy["owned"]["match"] == "exact" else None


def want(details):
    s = svc()
    s.store.want({k: details[k] for k in ("id", "title", "artist", "artist_id", "year", "kind", "cover")})
    publish("wanted.added", f"Wanted: {details['artist']} – {details['title']}", wanted_item(details))


def wanted_item(details):
    return {"id": f"wanted:{details['id']}", "title": f"{details['artist']} – {details['title']}",
            "image": f"/api/v1/images/cover/{details['id']}", "url": f"{base_url()}/wanted",
            "meta": {"album_id": details["id"], "artist": details["artist"], "album": details["title"]}}


def best_release(details):
    """The release the page would mark Best match: rated good, not failed before. None if there isn't one."""
    from .web import ORDER, failed_offers
    s = svc()
    items, _ = s.indexers.search(details["artist"], details["title"])
    failed = failed_offers(s.queue)
    rated = [(match.score(details, item), item) for item in items if item["key"] not in failed]
    good = [(verdict, item) for verdict, item in rated if verdict["tier"] == "good"]
    good.sort(key=lambda pair: (ORDER[pair[0]["tier"]], -pair[0]["score"], -pair[1]["size"]))
    return good[0][1] if good else None


def ready_to_download():
    s = svc()
    if not s.indexers.active():
        raise Refused("bad_request", "Encore has no indexers set up. Add one in Encore's Settings first.")
    if not s.sab.ready:
        raise Refused("bad_request", "Encore's SABnzbd isn't set up. Add it in Encore's Settings first.")
    if not engine.folders_ready():
        raise Refused("bad_request", "Encore's folders aren't set. Set them in Encore's Settings first.")


def get_album(details):
    """Queues the best release, or puts the album on Wanted when nothing rates good. Returns (queued, message)."""
    s = svc()
    name = f"{details['artist']} – {details['title']}"
    if active_job(details["id"]):
        return False, f"{name} is already in the queue."
    have = owned(details)
    if have:
        return False, f"You already have {name} ({have['folder']})."
    offer = best_release(details)
    if not offer:
        want(details)
        return False, f"Nothing good on your indexers for {name}, so it's on Wanted."
    s.queue.offers[offer["key"]] = offer
    s.queue.add(details, offer, True)
    return True, f"Queued {name}."


def act_want_album(body):
    details = details_of(album_id_from(body))
    name = f"{details['artist']} – {details['title']}"
    if details["id"] in svc().store.wanted_ids():
        return f"{name} is already on Wanted.", {"album_id": details["id"], "added": False}
    want(details)
    return f"Added {name} to Wanted.", {"album_id": details["id"], "added": True}


def act_get_album(body):
    ready_to_download()
    details = details_of(album_id_from(body))
    queued, message = get_album(details)
    return message, {"album_id": details["id"], "queued": queued}


def act_retry_job(body):
    from .web import Refusal, retry
    job_id = str(body.get("job_id") or "")
    job = svc().queue.get(job_id)
    if not job:
        raise Refused("not_found", "There's no job with that job_id. The downloads and failed feeds list them.", 404)
    try:
        retry(job_id)
    except Refusal as refusal:
        raise Refused("bad_request", f"{refusal.text}.") from None
    return f"Retrying {job['artist']} – {job['title']}.", {"job_id": job_id}


searching = threading.Lock()


def search_wanted(app):
    """Runs in its own thread: every wanted album that isn't queued or owned, one at a time."""
    with app.app_context(), app.test_request_context(base_url=getattr(app, "encore_base_url", "http://localhost/")):
        s = svc()
        queued = 0
        try:
            for album in s.store.wanted():
                try:
                    details = details_of(album["id"])
                    done, _ = get_album(details) if not active_job(album["id"]) else (False, "")
                    queued += done
                    s.store.db.execute("UPDATE wanted SET searched_at = ? WHERE album_id = ?", (time.time(), album["id"]))
                except SourceError as error:
                    log.warning("Wanted search skipped %s: %s", album.get("title"), error)
                time.sleep(WANTED_PAUSE)
            log.info("Wanted search queued %d album(s)", queued)
        finally:
            searching.release()


def act_search_wanted(body):
    from flask import current_app
    ready_to_download()
    count = len(svc().store.wanted_ids())
    if not count:
        return "Nothing is on Wanted.", {"albums": 0}
    if not searching.acquire(blocking=False):
        raise Refused("busy", "Wanted is already being searched. Try again when it's finished.", 503)
    app = current_app._get_current_object()
    app.encore_base_url = request.host_url
    threading.Thread(target=search_wanted, args=(app,), name="encore-wanted-search", daemon=True).start()
    return (f"Searching {count} wanted album{'' if count == 1 else 's'}; anything that rates good is queued.",
            {"albums": count})


ACTIONS = {
    "want_album": ("Add to Wanted", {"album_id": "str"}, None, act_want_album),
    "get_album": ("Get an album", {"album_id": "str"}, "30/hour", act_get_album),
    "retry_job": ("Retry a failed download", {"job_id": "str"}, None, act_retry_job),
    "search_wanted": ("Search for everything on Wanted", {}, "4/hour", act_search_wanted),
}


def log_action(name, message, ok):
    who = identity()
    svc().store.db.execute("INSERT INTO api_actions (at, action, message, token_name, ok) VALUES (?, ?, ?, ?, ?)",
                           (time.time(), name, message, who.name if who else "", int(ok)))


@bp.post("/api/v1/actions/<name>")
@standard("write")
def action(name):
    if name not in ACTIONS:
        return ApiCore.error("not_found", f"There's no action called '{name}'. See /api/v1/meta for the actions.", 404)
    store, who = svc().store, identity()
    key = request.headers.get("Idempotency-Key")
    owner = str(who.token_id) if who.kind == "token" else "session"
    if key is not None:
        if not 0 < len(key) <= 100:
            return ApiCore.error("bad_request", "Idempotency-Key is 1 to 100 characters.", 400)
        seen = store.one("SELECT status, body FROM api_idempotency WHERE token = ? AND key = ? AND at > ?",
                         owner, key, time.time() - 86400)
        if seen:
            return _json(json.loads(seen["body"]), seen["status"])
    limited = core().over_limit(name)
    if limited is not None:
        log_action(name, json.loads(limited.get_data())["error"], False)
        return limited
    body = request.get_json(silent=True) if request.data else {}
    if not isinstance(body, dict):
        return ApiCore.error("bad_request", "Send a JSON object as the body.", 400)
    try:
        message, result = ACTIONS[name][3](body)
    except Refused as refused:
        log_action(name, refused.message, False)
        answer = {"error": refused.message, "code": refused.code}
        if key is not None and refused.status < 500:
            remember(owner, key, refused.status, answer)
        return ApiCore.error(refused.code, refused.message, refused.status)
    except SourceError as error:
        log_action(name, f"{error}.", False)
        return ApiCore.error("upstream", f"{error}.", 502)          # not remembered: retrying the key may work
    log_action(name, message, True)
    answer = {"ok": True, "message": message, "result": result}
    if key is not None:
        remember(owner, key, 200, answer)
    return _json(answer)


def remember(owner, key, status_code, answer):
    svc().store.db.execute("INSERT OR REPLACE INTO api_idempotency (token, key, at, status, body) VALUES (?, ?, ?, ?, ?)",
                           (owner, key, time.time(), status_code, json.dumps(answer)))


# ---------------------------------------------------------------- events and webhooks

wake_deliveries = threading.Event()


def publish(kind, title, item):
    """One event: a row for /api/v1/events, and a delivery for each webhook that wants it."""
    store = svc().store
    at = iso(time.time())
    cursor = store.db.execute("INSERT INTO events (type, at, title, item) VALUES (?, ?, ?, ?)",
                              (kind, at, title, json.dumps(item)))
    event = {"id": cursor.lastrowid, "type": kind, "at": at, "title": title, "item": item}
    body = json.dumps(event, separators=(",", ":"))
    for hook in store.rows("SELECT id, event_types FROM webhooks"):
        types = json.loads(hook["event_types"] or "[]")
        if not types or kind in types:
            store.db.execute("INSERT INTO webhook_deliveries (webhook_id, event_id, body, next_at) VALUES (?, ?, ?, ?)",
                             (hook["id"], event["id"], body, time.time()))
            wake_deliveries.set()


def on_job(app):
    """The queue's watcher: events for every change that matters, and the arrivals history."""
    def watch(job, old):
        with app.app_context(), app.test_request_context(base_url=getattr(app, "encore_base_url", "http://localhost/")):
            name, state = f"{job['artist']} – {job['title']}", job["state"]
            if state == "queued" and old in (None, "failed"):
                publish("job.queued", f"Queued {name}", job_item(job))
            elif state == "downloading" and old == "queued":
                publish("download.started", f"Downloading {name}", job_item(job))
            elif state == "done":
                svc().store.db.execute("INSERT OR REPLACE INTO arrivals (job_id, at, data) VALUES (?, ?, ?)",
                                       (job["id"], job.get("finished") or time.time(), json.dumps(engine.public(job))))
                where = "your library" if job.get("where") == "library" else "MusicImport"
                publish("download.finished", f"{name} is in {where}", job_item(job))
            elif state == "failed":
                publish("download.failed", f"{name} failed", job_item(job))
    return watch


def rows_after(store):
    """For the stream, which runs after the request has handed over: it holds the store, not the app."""
    def after(last_id):
        rows = store.rows("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT 200", last_id)
        return [{"id": r["id"], "type": r["type"], "at": r["at"], "title": r["title"], "item": json.loads(r["item"])} for r in rows]
    return after


@bp.get("/api/v1/events")
@standard("read")
def events():
    release = core().streams.take()
    if release is None:
        return ApiCore.error("busy", "Too many open event streams. Try again shortly.", 503, 30)
    asked = request.headers.get("Last-Event-ID") or request.args.get("last_event_id")
    try:
        last = int(asked) if asked else None
    except ValueError:
        last = None
    if last is None:
        newest = svc().store.one("SELECT MAX(id) AS id FROM events")["id"]
        last = newest or 0
    else:
        oldest = svc().store.one("SELECT id FROM events WHERE id > ? ORDER BY id DESC LIMIT 1 OFFSET 199", last)
        last = max(last, oldest["id"] - 1) if oldest else last
    stopping, after = core().stopping, rows_after(svc().store)

    def stream():
        try:
            yield from event_stream(after, last, stopping=stopping)
        finally:
            release()

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def deliver_once(store):
    due = store.rows("SELECT d.*, w.url, w.secret FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id "
                     "WHERE d.done = 0 AND d.next_at <= ? ORDER BY d.id LIMIT 20", time.time())
    for row in due:
        body = row["body"].encode()
        try:
            response = requests.post(row["url"], data=body, timeout=WEBHOOK_TIMEOUT_S,
                                     headers={"Content-Type": "application/json", "User-Agent": f"Encore/{__version__}",
                                              "X-Signature": sign(body, row["secret"])})
            problem = None if 200 <= response.status_code < 300 else f"HTTP {response.status_code}"
        except requests.RequestException as error:
            problem = type(error).__name__
        if problem is None:
            store.db.execute("UPDATE webhook_deliveries SET done = 1, tries = tries + 1 WHERE id = ?", (row["id"],))
            store.db.execute("UPDATE webhooks SET last_ok_at = ? WHERE id = ?", (time.time(), row["webhook_id"]))
            continue
        tries = row["tries"] + 1
        later = RETRY_AFTER_S[tries - 1] if tries <= len(RETRY_AFTER_S) else None
        store.db.execute("UPDATE webhook_deliveries SET tries = ?, next_at = ?, done = ? WHERE id = ?",
                         (tries, time.time() + (later or 0), 0 if later else 2, row["id"]))
        store.db.execute("UPDATE webhooks SET last_error = ?, last_error_at = ? WHERE id = ?",
                         (problem, time.time(), row["webhook_id"]))


def deliveries(app):
    while True:
        wake_deliveries.wait(15)
        wake_deliveries.clear()
        try:
            with app.app_context():
                deliver_once(svc().store)
        except Exception:
            log.exception("Webhook delivery failed")


def tidy(app):
    """Hourly: old events (keeping the last 200), old idempotency keys, deliveries and actions, the image cache."""
    while True:
        try:
            with app.app_context():
                db = svc().store.db
                hour_ago = iso(time.time() - 3600)
                db.execute("DELETE FROM events WHERE at < ? AND id <= (SELECT COALESCE(MAX(id), 0) - ? FROM events)",
                           (hour_ago, KEEP_EVENTS))
                db.execute("DELETE FROM api_idempotency WHERE at < ?", (time.time() - 86400,))
                db.execute("DELETE FROM webhook_deliveries WHERE done != 0 AND next_at < ?", (time.time() - 86400,))
                db.execute("DELETE FROM api_actions WHERE id <= (SELECT COALESCE(MAX(id), 0) - 500 FROM api_actions)")
                prune_image_cache(image_cache())
        except Exception:
            log.exception("API tidy failed")
        time.sleep(3600)


# ---------------------------------------------------------------- the downloads display page

def display_options():
    def number(name, low, high, default):
        try:
            return max(low, min(high, float(request.args.get(name) or default)))
        except ValueError:
            return default
    accent = request.args.get("accent") or "blue"
    return {"theme": "light" if request.args.get("theme") == "light" else "dark", "scale": number("scale", 0.5, 3, 1),
            "accent": accent if accent in ("violet", "blue", "graphite", "teal", "fuchsia") else "blue",
            "static": request.args.get("static") in ("1", "true")}


def downloads_view(token):
    jobs = jobs_now()
    tail = f"&token={quote(token)}" if token else ""
    cover_of = lambda path: f"{path}?w=160{tail}" if path else None   # noqa: E731
    running = [j for j in jobs if j["state"] in RUNNING][:3]
    waiting = [j for j in jobs if j["state"] == "queued"][:5]
    arrivals = [arrival_item(r) for r in svc().store.rows("SELECT * FROM arrivals ORDER BY at DESC LIMIT 4")]
    return {"status": status_body(),
            "now": [{"title": j["title"], "artist": j["artist"], "text": job_text(j), "cover": cover_of(image_for(j)),
                     "percent": round(j.get("percent") or 0) if j["state"] in ("downloading", "filing") else None}
                    for j in running],
            "next": [{"title": j["title"], "artist": j["artist"]} for j in waiting],
            "arrived": [{"title": a["meta"]["album"], "artist": a["meta"]["artist"], "when": a["at_label"],
                         "cover": cover_of(a["image"]), "tone": a["tone"]} for a in arrivals]}


@bp.get("/display/downloads")
@display
def display_downloads():
    who = identity()
    token = request.args.get("token", "") if who.from_url else ""
    view = downloads_view(token)
    if request.args.get("fragment") == "1":
        return render_template("display/downloads_body.html", **view)
    keep = {k: v for k, v in request.args.items() if k != "fragment"}
    query = "&".join(f"{k}={quote(v)}" for k, v in keep.items())
    return render_template("display/downloads.html", **view, opts=display_options(), title="Encore · Downloads",
                           stylesheets=[url_for("static", filename="design/uniform.css"),
                                        url_for("static", filename="encore.css")],
                           page={"refresh_s": FEED_REFRESH["downloads"]},
                           fragment_url=f"/display/downloads?{query}{'&' if query else ''}fragment=1",
                           events_url=f"/api/v1/events{'?token=' + quote(token) if token else ''}",
                           event_names=["job.queued", "download.started", "download.finished", "download.failed"])


# ---------------------------------------------------------------- Settings: tokens and webhooks

shown_once = {}          # a nonce -> a new token or secret, for the one page that shows it


def show_once(kind, value, name):
    nonce = secrets.token_urlsafe(12)
    shown_once[nonce] = {"kind": kind, "value": value, "name": name, "at": time.time()}
    for old in [k for k, v in shown_once.items() if time.time() - v["at"] > 300]:
        shown_once.pop(old, None)
    return nonce


def ago(ts):
    if not ts:
        return None
    seconds = time.time() - ts
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)}m ago"
    if seconds < 129600:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


def masked(url):
    parts = urlsplit(url)
    tail = parts.path[-4:] if len(parts.path) > 4 else ""
    return f"{parts.scheme}://{parts.netloc}/…{tail}"


def settings_context():
    """What Settings needs for the shared API tokens and Webhooks cards."""
    store = svc().store
    new = shown_once.pop(request.args.get("shown", ""), None)
    hooks = []
    for w in store.rows("SELECT * FROM webhooks ORDER BY id"):
        pending = store.one("SELECT COUNT(*) AS n FROM webhook_deliveries WHERE webhook_id = ? AND done = 0", w["id"])["n"]
        failed = bool(w["last_error_at"] and w["last_error_at"] > (w["last_ok_at"] or 0))
        status = (f"failed {ago(w['last_error_at'])}: {w['last_error']}" if failed
                  else f"sent {ago(w['last_ok_at'])}" if w["last_ok_at"] else "nothing sent yet")
        hooks.append({"id": w["id"], "name": w["name"], "masked_url": masked(w["url"]),
                      "event_types": json.loads(w["event_types"] or "[]"), "status": status, "failed": failed,
                      "pending": pending})
    return {
        "api_tokens": [{"id": t["id"], "name": t["name"], "scope": t["scope"], "last_used": ago(t["last_used_at"])}
                       for t in store.rows("SELECT * FROM api_tokens WHERE revoked_at IS NULL ORDER BY id")],
        "api_token_new": {"token": new["value"], "name": new["name"]} if new and new["kind"] == "token" else None,
        "api_urls": {"create": "/settings/api/tokens", "revoke": "/settings/api/tokens/{id}/revoke"},
        "api_displays": [{"title": "Downloads", "url": "/display/downloads"}],
        "api_actions": [{"action": a["action"], "message": a["message"], "token_name": a["token_name"],
                         "when": ago(a["at"]), "ok": bool(a["ok"])}
                        for a in store.rows("SELECT * FROM api_actions ORDER BY id DESC LIMIT 8")],
        "webhooks": hooks,
        "webhook_new": {"secret": new["value"], "name": new["name"]} if new and new["kind"] == "secret" else None,
        "webhook_event_types": EVENT_TYPES,
        "webhook_urls": {"create": "/settings/webhooks", "secret": "/settings/webhooks/{id}/secret",
                         "delete": "/settings/webhooks/{id}/delete"},
    }


def back(anchor, nonce=None):
    return redirect(f"/settings{'?shown=' + nonce if nonce else ''}#{anchor}", 303)


@bp.post("/settings/api/tokens")
def create_token():
    name = " ".join(request.form.get("name", "").split())[:60] or "Unnamed"
    scope = "write" if request.form.get("scope") == "write" else "read"
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    svc().store.db.execute("INSERT INTO api_tokens (name, scope, hash, created_at) VALUES (?, ?, ?, ?)",
                           (name, scope, token_hash(token), time.time()))
    return back("api", show_once("token", token, name))


@bp.post("/settings/api/tokens/<int:token_id>/revoke")
def revoke_token(token_id):
    svc().store.db.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ?", (time.time(), token_id))
    return back("api")


@bp.post("/settings/webhooks")
def create_webhook():
    name = " ".join(request.form.get("name", "").split())[:60] or "Webhook"
    url = request.form.get("url", "").strip()
    if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
        return back("webhooks")
    types = [t for t in request.form.getlist("event_types") if t in EVENT_TYPES]
    secret = secrets.token_urlsafe(32)
    svc().store.db.execute("INSERT INTO webhooks (name, url, secret, event_types, created_at) VALUES (?, ?, ?, ?, ?)",
                           (name, url, secret, json.dumps(types), time.time()))
    return back("webhooks", show_once("secret", secret, name))


@bp.post("/settings/webhooks/<int:hook_id>/secret")
def new_webhook_secret(hook_id):
    row = svc().store.one("SELECT name FROM webhooks WHERE id = ?", hook_id)
    if not row:
        return back("webhooks")
    secret = secrets.token_urlsafe(32)
    svc().store.db.execute("UPDATE webhooks SET secret = ? WHERE id = ?", (secret, hook_id))
    return back("webhooks", show_once("secret", secret, row["name"]))


@bp.post("/settings/webhooks/<int:hook_id>/delete")
def delete_webhook(hook_id):
    db = svc().store.db
    db.execute("DELETE FROM webhook_deliveries WHERE webhook_id = ?", (hook_id,))
    db.execute("DELETE FROM webhooks WHERE id = ?", (hook_id,))
    return back("webhooks")


# ---------------------------------------------------------------- wiring

def install(app, s):
    from .du_flask import git_sha
    c = init_api(app, name="encore", title="Encore", version=__version__, sha=lambda: git_sha(app.root_path),
                 accent="blue", icon="/static/img/icon.svg", time_zone=lambda: TIME_ZONE, check_token=check_token,
                 is_admin_session=is_admin_session, csrf_ok=csrf_ok)
    for name, (title, _, params) in FEEDS.items():
        c.add_feed(name, title, FEED_REFRESH[name], params)
    for name, (title, body, limit, _) in ACTIONS.items():
        c.add_action(name, title, body, limit=limit)
    c.add_display("downloads", "Downloads", DISPLAY_SIZES)
    c.add_events(EVENT_TYPES)
    c.unmetered_endpoints |= {"encore_api.image", "encore_api.events"}
    app.register_blueprint(bp)
    app.jinja_env.globals["csrf_token"] = csrf_value
    s.queue.watchers.append(on_job(app))
    s.publish = publish

    @app.before_request
    def remember_address():
        """Events from the queue's own thread link back to Encore, so keep the address people use."""
        if request.cookies or request.path.startswith(("/api/v1/", "/display/")):
            app.encore_base_url = request.host_url


def start(app):
    """From start_workers(): webhook delivery, the hourly tidy, and ending streams on SIGTERM."""
    stop_streams_on_sigterm(core_of(app))
    threading.Thread(target=deliveries, args=(app,), name="encore-webhooks", daemon=True).start()
    threading.Thread(target=tidy, args=(app,), name="encore-api-tidy", daemon=True).start()


def core_of(app):
    return app.extensions["du_api"]
