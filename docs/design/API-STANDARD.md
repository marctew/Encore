# API standard

How Marc's apps let other things read and drive them: Docket's briefs, Vital Signs screens, the ESP32 panels, the widget display, Home Assistant, n8n and TRMNL. `APP-STANDARD.md` says how an app is built; this says what its outside API looks like, so a reader written once works against every app.

**Status:** draft 4, 6 Nov 2026. Draft 3 was released with the shared code (section 12) in v1.10. Draft 4 adds **inbound items** (things another app keeps in this one, first for Muddy Paws' reminders in Docket), times sent *to* an app, per-action limits, and events that say which app an item came from (section 18). Its shared-code additions are in v1.11 (section 12). What it means for Binge Stats, already running draft 3, is in section 19. Decisions taken with Marc on 5 Nov 2026 are marked **(decided)**. Binge Stats built all of it first, as the pilot (decided), and is running it; draft 3 takes in what the build found (section 17). The shared code was lifted out of Binge (sections 12 and 14).

## 1. What it's for

| Reader | Where it runs | What it needs |
|---|---|---|
| **Docket briefs** (Claude, via Docket's `feed_fetch`) | Docket's server, on the LAN | Feeds and status, read-only. The token stays in Docket; Claude never sees it. |
| **Vital Signs** pages | Its server's data proxy, then Chromium on the Pis | Status, feeds, images, display pages; live updates for alerts. |
| **ESP32 panels** (through the PC panel agent) | The PC | Small JSON: short labels, numbers, pre-formatted times. Actions for buttons. |
| **Home Assistant** | LAN | Status as REST sensors, actions as REST commands, webhooks for automations. |
| **TRMNL** and other cloud displays | TRMNL's cloud, polling over the internet | Small JSON with the token in the URL. Only for apps Marc puts on the tunnel. |
| **The widget display** and any kiosk | A browser on the LAN | Display pages that just need a URL. |

What it isn't: a replacement for an app's own page JSON. Each app keeps its private `/api/...` routes for its own UI, with the session cookie and CSRF (`APP-STANDARD.md` 4). The public contract lives only under **`/api/v1/`**, plus `/display/` and `/healthz`.

**Apps that already have a `/api/v1/`** (Binge Stats) keep every existing route exactly as it is (path, shape and error shape), because live readers depend on them. The standard's routes are added beside them. The old routes do pick up the shared token handling (URL tokens, lockout, ETags, rate limit), since none of that changes their bodies.

## 2. The shape of it

Every app that adopts the standard has these. Anything an app doesn't have is simply missing from `meta`, not an error.

| Path | Method | Token | What |
|---|---|---|---|
| `/healthz` | GET | none | Unchanged from `APP-STANDARD.md`: `{"ok": true, "busy": [...]}`. Says nothing about contents. |
| `/api/v1/meta` | GET | read | Who the app is and what it offers: version, accent, icon, and the list of feeds, actions, displays and events. Readers use it to build pickers. |
| `/api/v1/status` | GET | read | The one-glance summary: a state, a headline and a few numbers. Every display and HA sensor starts here. |
| `/api/v1/feeds/<name>` | GET | read | A list of items in the common item shape (section 5). |
| `/api/v1/images/<kind>/<id>` | GET | read | Artwork or a thumbnail, resized by the app. |
| `/api/v1/actions/<name>` | POST | write | One named thing to do, with a JSON body. |
| `/api/v1/inbound/items/<key>` | PUT, GET, DELETE | app | An item another app keeps here under its own key: create or update, read, archive (section 7). |
| `/api/v1/inbound/items/<key>/done` | POST | app | Mark that item done. |
| `/api/v1/events` | GET | read | Server-sent events: live changes for screens. |
| `/display/<name>` | GET | read | A ready-made HTML card in the app's look, for kiosks and the widget display. |

Readers send `Accept: application/json` (except for images, events and display pages). Every JSON answer is UTF-8, `snake_case`, and is an object at the top level, never a bare list, so a field can be added later without breaking anyone.

## 3. Tokens

- **Made in the app's Settings,** on an **API tokens** card (a shared partial in this repo), with a name ("Vital Signs desk", "TRMNL kitchen") and a scope: **read** or **write**. A write token can also read.
- **Shown once,** stored only as a SHA-256 hash, with `created_at` and `last_used_at` shown on the card. Revoke is one click. This is how Binge Stats already does it; it's the model.
- **Sent as** `Authorization: Bearer <token>`.
- **Read tokens may also go in the URL** as `?token=…` **(decided)**, because TRMNL's cloud polling and simple firmware can't always set a header. Write tokens are refused in the URL with `400 {"error": "Write tokens go in the Authorization header, never the URL."}`. URLs end up in logs, so a URL token should be a display's own read token, easy to revoke.
- **Tokens never reach a log.** gunicorn's default access log has the query string (`%(r)s`, `%(q)s`) and the referrer (`%(f)s`), so every app sets `--access-logformat '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(a)s"'` (`APP-STANDARD.md` 3): the path without the query string, and no referrer. The app's own logging never prints `request.url` or `request.full_path` for these paths, and `/display/` pages send `Referrer-Policy: no-referrer`.
- **A signed-in admin** (the app's own session cookie) can read every `/api/v1/` GET too, so the app's own pages and its `/display/` previews work without a token. In an app with viewer or other non-admin logins (Binge Stats), only an admin session counts; any other session is treated as no session. Writes from a session still need CSRF.
- **Bad tokens:** `401 {"error": "That token isn't valid. Make a new one in Settings → API tokens."}`. Ten bad tokens from one address in a minute lock that address out for 15 minutes with `429` and `Retry-After`, the same rule as Docket's MCP. The address is `CF-Connecting-IP` when it's there, else the client IP: an app with no trusted proxies sees every tunnel request coming from cloudflared, so keyed by that, one stranger's bad tokens would lock out every tunnel reader. Cloudflare overwrites the header, so it can't be faked from outside, and faking it on the LAN only dodges the lockout. A good token with the wrong scope gets `403`.
- **Secrets an app holds for others** (Plex tokens, indexer keys, a mail password) never appear in any `/api/v1/` answer, image URL or display page.
- **App tokens.** A write token can also **belong to an app** ("Muddy Paws"), set when it's made. Only an app token can use `/api/v1/inbound/`, and the items it creates belong to that app: no other token can read or change them, and another app using the same key gets a separate item. Any other token gets `403` there, code `forbidden`: "Only an app's own token can keep items here. Make one in Settings → API tokens and say which app it's for." An app token is still a write token everywhere else. Its hint, last use and app name show on the tokens card like any other.

## 4. Exposure

- **LAN by default (decided).** `/api/v1/`, `/display/` and `/healthz` answer on the app's LAN address.
- **Through the tunnel only per app, by Marc's choice (decided).** If an app's hostname is on the Cloudflare tunnel, its `/api/v1/` works there too, always with a token. An app can switch that off with `API_PUBLIC=0` in its env, which makes it refuse `/api/v1/` and `/display/` requests that came through the proxy (`CF-Connecting-IP` present) with `403`.
- **Through the tunnel, readers aren't browsers, so Cloudflare needs a rule (6 Nov 2026).** Browser Integrity Check is on for the whole `marctew.co.uk` zone, and it refuses anything that doesn't look like a browser (TRMNL, Home Assistant away from home, scripts, `check_api.py`) with `403` and `error code: 1010`, before the app sees the request. So every app whose API goes through the tunnel gets a **Configuration Rule** in the zone (Rules → Configuration Rules): when `http.host eq "<app>.marctew.co.uk" and (starts_with(http.request.uri.path, "/api/v1/") or starts_with(http.request.uri.path, "/display/"))`, set Browser Integrity Check **off**. Nothing else changes: the rest of the app keeps the check, and the API keeps its tokens. Encore and Binge Stats have one. Check from outside with `scripts/check_api.py https://<app>.marctew.co.uk <read token>`. `APP-STANDARD.md` 13 lists this rule with the Cache Rule every app on the tunnel needs.
- **Header names arrive in lower case through Cloudflare** (`etag`, `referrer-policy`), so readers look headers up case-insensitively. `check_api.py` does from v1.15; before that it reported a missing ETag and Referrer-Policy through the tunnel that were really there.
- **No CORS by default.** Browser readers on another origin go through a server (Vital Signs' data proxy). An app may list allowed origins in Settings if one is ever needed.

## 5. Common shapes

### Status

```json
{
  "app": "encore",
  "state": "ok",
  "headline": "2 albums downloading",
  "detail": "Next: Rumours (1977), 64%",
  "counts": [
    {"key": "downloading", "label": "Downloading", "value": 2},
    {"key": "queued", "label": "Queued", "value": 5},
    {"key": "failed_today", "label": "Failed today", "value": 0, "tone": "danger"}
  ],
  "url": "https://encore.marctew.co.uk/",
  "generated_at": "2026-11-05T22:40:00Z",
  "refresh_s": 30
}
```

- `state` is one of `ok`, `busy`, `warning`, `error`, `off`. It maps straight to the design's tones (success, info, warning, danger, muted) so every screen colours it the same.
- `headline` is one short sentence that makes sense on its own on a 2-inch screen. `detail` is optional and can be longer.
- `counts` is at most 6 entries. `key` is stable for machines (HA sensors read `counts[key=...]`), `label` is for people. `tone` is optional.
- `refresh_s` is how often a poller should come back. Readers that can't do events use it.

### Feeds and items

```json
{
  "feed": "arrivals",
  "title": "Just arrived",
  "items": [
    {
      "id": "plex:58213",
      "title": "Slow Horses",
      "subtitle": "S5 E3 · Missing Persons",
      "at": "2026-11-05T19:12:00Z",
      "at_label": "Today 19:12",
      "tone": "accent",
      "image": "/api/v1/images/poster/58213",
      "url": "http://192.168.4.57:8181/shows/412",
      "meta": {"show_id": 412, "season": 5, "episode": 3}
    }
  ],
  "next_cursor": null,
  "generated_at": "2026-11-05T22:40:00Z",
  "refresh_s": 300
}
```

- Every item has `id` (stable, unique within the feed) and `title`. Everything else is optional, but when a field fits one of these names it uses it.
- `at` is UTC ISO 8601 ending in `Z`. `at_label` is the same moment already written for a person in the app's time zone ("Today 19:12", "Sat 7 Nov", "in 2 h"), because an ESP32 or a Liquid template shouldn't have to do time zones.
- `image` is a path on the same app (section 6), never a third-party URL that carries a token.
- `url` opens the thing in the app, for a tap on a screen or a link in a brief.
- `meta` holds the feed's own fields. It's documented per feed and is where app-specific detail goes, so the top level stays the same for everyone.
- **Query parameters:** every feed accepts `limit` (default 20, at most 100). A feed ordered by time, newest first (arrivals, history, alerts), also accepts `since` (ISO time: items with `at` after it) and `cursor` (from `next_cursor`). A feed in some other order (a live queue, what's playing now, an air-date schedule) is answered whole up to `limit`, with `next_cursor: null`, and ignores `since` and `cursor`; its documentation says so. A feed may add its own parameters, listed in `meta`.

### Errors

```json
{"error": "There's no feed called 'arivals'.", "code": "not_found"}
```

This shape is for the standard's routes. An app's older `/api/v1/` routes keep the error shape they already had (section 1). `error` is a sentence for a person (`APP-STANDARD.md` 4). `code` is for machines: `bad_request`, `unauthorised`, `forbidden`, `not_found`, `rate_limited`, `busy`, `upstream` (the app couldn't reach Plex, SABnzbd...), `error`. `429` and `503` always carry `Retry-After`.

### Times you send

Times in answers are UTC (`...Z`) with an `at_label`, as above. Times **sent to** an app (an action's body, an inbound item) follow what the thing is:

- **A wall-clock time** (a due date, a reminder, a schedule: something that should stay at 09:00 when the clocks change) is local to the app's `time_zone` (in `meta`): `"2026-12-24 09:00"`, or `"2026-12-24"` for all day. The app answers with the same local string and the UTC moment beside it (`due` and `due_at`).
- **A moment that happened** (a dose was logged, a play finished) is ISO 8601 UTC with `Z`.
- Anything else is `400 bad_request` with a sentence giving the right form. An action's body in `meta` marks each time field `"local"` or `"utc"`.

### Meta

```json
{
  "app": "binge-stats",
  "name": "Binge Stats",
  "version": "1.0.0",
  "sha": "ba66f41",
  "api": 1,
  "accent": "violet",
  "icon": "/static/icons/icon-192.png",
  "time_zone": "Europe/London",
  "feeds": [{"name": "arrivals", "title": "Just arrived", "refresh_s": 300, "params": {"kind": "movie | show"}}],
  "actions": [{"name": "mark_watched", "title": "Mark watched", "body": {"item_id": "int"}},
              {"name": "notify", "title": "Send a push", "body": {"title": "str", "body": "str", "url": "str?"}, "limit": "10/hour"}],
  "inbound": {"kind": "task", "fields": {"title": "str", "notes": "str?", "due": "local?", "remind": "bool?", "url": "str?", "project": "str?"}},
  "displays": [{"name": "now-playing", "title": "Now playing", "sizes": ["320x240", "800x480", "1920x1080"]}],
  "events": ["arrival", "playback.start", "playback.stop"]
}
```

## 6. Images

- `GET /api/v1/images/<kind>/<id>?w=320` (or `h=`). The app fetches the original with its own credentials, resizes it, and answers `image/webp` (or `image/jpeg` if the reader's `Accept` doesn't list WebP, for ESP32 decoders).
- Widths snap to 80, 160, 320, 640 and 1280, so caches stay small. Resized images are cached on disk in `/var/cache/<app>/images/` (`APP-STANDARD.md` 3), or under the app's data volume for an app in Docker (Binge Stats: `/data/image-cache/`). Each file is named by a hash of kind, id, width, format, quality and grey. They're served with `Cache-Control: private, max-age=86400` and an ETag (`private`, not `public`, since 9 Nov 2026: an image needs a token like everything else here, and a shared cache such as the tunnel's Cloudflare would hand a `public` one to a request without one), and pruned oldest-first (by mtime) above 500 MB by a background job.
- `?fmt=jpeg&q=70` is allowed for devices that need it. `?gray=1` gives greyscale for e-ink.
- An image path in a feed works with the same token the feed was read with, so a display can load it straight away.

## 7. Actions

- `POST /api/v1/actions/<name>` with a JSON body. Write token only, in the header.
- **`Idempotency-Key`** header (any string up to 100 characters): repeating a request with the same key within 24 hours gives the first answer again and does nothing twice. Panels and HA retry, so this matters.
- Answer: `{"ok": true, "message": "Queued Rumours (1977).", "result": {...}}`. `message` is short enough for a toast or a panel's status line.
- Only actions listed in `meta` exist. They're named for what they do (`add_task`, `queue_album`, `mark_watched`, `skip_track`), not generic `update` endpoints, and each is documented with its body.
- Anything destructive in the app's own UI (delete, restore) isn't offered as an action unless Marc asks for that one specifically.
- Every action that runs, refused or not, is logged in the app's action log with the token's name, so "who did that" always has an answer. A repeat answered from an `Idempotency-Key` isn't logged again, since nothing ran. An upstream failure (`502`) isn't remembered against its key, so it can be retried with the same one.
- An admin signed in to the app can call actions from its own pages with the session cookie and `X-CSRF-Token`; the log names them as "Admin session".
- **Per-action limits.** An action can have its own limit on top of the token's 120 a minute, for things that reach Marc (a push) or cost something: `"limit": "10/hour"` in `meta`. Over it, `429 rate_limited` with `Retry-After` and a sentence ("That's 10 pushes this hour. The next one can go at 14:20."). Counted per token, in memory, per worker, like the others (section 10).

### Inbound items: things another app keeps here

Some apps need to keep their own things *inside* another app and keep them in step: Muddy Paws puts each dog's next worming or flea dose into Docket as a reminder, moves it when a dose is logged early, and closes it when the dose is logged in Paws first. Actions don't fit that, because the sender has to name the same thing every time without making duplicates. So an app that accepts these offers **inbound items**, keyed by the sender's own id.

- **Who:** an app token only (section 3). Everything is scoped to that token's app.
- **The key** is the sender's own stable id for the thing: `[A-Za-z0-9._:-]`, 1 to 100 characters (`treatment-dog1-worming`). The same app and key always mean the same item, from one cycle to the next.
- **`PUT /api/v1/inbound/items/<key>`** creates or updates, and is safe to call every time anything changes:
  - a new key creates the item (`201`); a known key updates it (`200`); the body is the app's documented fields (`meta.inbound.fields`), and `title` is needed to create;
  - fields left out are left as they are;
  - a **wall-clock** field that moves (a due date) moves whatever hangs off it (a reminder), and drops a snooze already applied;
  - if Marc already marked it **done**, a PUT with a **later** due date reopens it for that date (that's the next cycle); the same or an earlier date leaves it done and answers `200` with `status: "done"`;
  - an **archived** item is brought back by a PUT, since the thing applies again.
- **`GET .../<key>`** reads it. **`POST .../<key>/done`** marks it done and cancels anything pending on it (idempotent, `200`). **`DELETE .../<key>`** archives it (apps never hard-delete; `200`). All three give `404` for a key this app hasn't used, including one only another app has.
- **The answer** is the item: `{"key", "id", "status": "open | done | archived", "title", "due", "due_at", "at_label", "url", "updated_at", "meta": {...}}`, where `id` is the receiving app's own id and `meta` holds its extra fields (Docket: `snoozed_until`, `project`).
- **`url`** is a link back into the sending app. The receiving app shows it on the item and uses it where Marc would tap (a push's click), so the reminder opens the dog's page in Paws.
- **Shown as from that app:** the receiving app marks these items with the app's name ("from Muddy Paws") so Marc can tell them apart.
- **Logged** in the action log like actions, with the token's name and the key.
- **Telling the sender about changes** is done with events and webhooks, not a separate callback: the receiving app publishes an event when Marc changes an inbound item (`task.done`, `task.reopened`, `task.archived`), with the item's `source` (section 8), and the sender subscribes with a webhook filtered to its own items. Changes the sender made itself through `/api/v1/inbound/` publish no event to it, so it never hears its own writes back.

## 8. Live updates (decided: push from the start)

### Events stream

- `GET /api/v1/events` is **server-sent events** (`text/event-stream`). Read token (URL or header).
- Each event: `event: <type>`, `id: <row id>`, `data: {"type": "...", "at": "...Z", "title": "...", "item": {...item shape...}}`. Types are dotted and listed in `meta` (`arrival`, `download.finished`, `task.due`, `watch.fired`).
- An event about an **inbound item** also has `"source": {"app": "muddy-paws", "key": "treatment-dog1-worming"}`, so the app that owns it can tell it's one of its own. An app with inbound items adds a nullable `source_app` column to its `events` table, for the webhook filter below.
- **Events go through SQLite, not memory.** Apps can have more than one gunicorn worker, and a separate worker process where events actually start (Binge Stats has both). So `publish()` writes a row to a small table, from whichever process sees the event, and each open stream reads new rows from it:

  ```sql
  CREATE TABLE events (
    id    INTEGER PRIMARY KEY,   -- the SSE id and Last-Event-ID
    type  TEXT NOT NULL,         -- dotted, e.g. playback.start
    at    TEXT NOT NULL,         -- UTC ...Z (an app that stores epoch seconds may store those, and convert at the edge)
    title TEXT NOT NULL,
    item  TEXT NOT NULL          -- JSON, the item shape
  );
  CREATE INDEX events_at ON events (at);
  ```

- A stream checks for new rows once a second, with a read-only query on its own connection, and sends `: ping` every 25 seconds so proxies don't close it.
- Reconnecting with `Last-Event-ID` (the header, or `?last_event_id=`) replays up to 200 rows after that id. Without one, nothing is replayed. A background job prunes rows older than an hour but always keeps the last 200.
- **Capacity:** at most **8 open streams per app, split across its gunicorn workers** (one worker: 8; two workers: 4 each). Beyond that, `503` with `Retry-After: 30`. Each stream holds a thread, so an app that adopts events raises `--threads` to leave at least 8 free for everything else.
- On `SIGTERM` the stream ends, so `scripts/update.sh` restarts don't hang.
- Through Cloudflare, streams work but are closed after about 100 seconds without data; the 25-second ping prevents that.

### Webhooks out

For Home Assistant and n8n, which handle incoming webhooks better than streams:

- Settings → **Webhooks**: a URL, the event types to send, and a secret. An app with inbound items also offers **Only items from** (one connected app, or any), so a sending app's webhook gets back only the events about its own items. This is how Muddy Paws hears that Marc ticked a reminder off in Docket.
- Docket-style delivery, always from a background job, never inside a request: each event makes one delivery per matching webhook, POSTed as JSON (the same `data` as the stream) with `X-Signature: sha256=<hex HMAC-SHA256 of the raw body with the secret>` and a 10-second timeout. A failure is retried after 1, 3 and 6 minutes, then given up, with the last success and last error shown on the webhook's row in Settings.
- Home Assistant's own webhook trigger (`/api/webhook/<id>`) is the expected target. Its id is the secret there, so the URL is stored like a password and never shown again in full.

### Push to Marc's phone

Unchanged: apps that need to tell Marc something use the self-hosted ntfy server, each with its own write-only token. Events and webhooks are for machines and screens; ntfy is for Marc.

## 9. Display pages

- `GET /display/<name>` returns a full HTML page in the app's look (DESIGN-SPEC tokens, its accent), sized to fill the viewport, with no sidebar, no login prompt and no scroll bars.
- Parameters: `token` (read), `theme=dark|light` (default dark), `size=800x480` (a hint for layout breakpoints; the page still fills whatever it's given), `scale=1.5` for far-away screens, `static=1` to render once with no JavaScript (e-ink and screenshot services), and `accent=` to override the app's accent so a wall of screens can match.
- Unless `static=1`, the page reloads **its own body** (the same route with `fragment=1`) every `refresh_s`, and straight away when a relevant event comes in on `/api/v1/events`. One server-side template renders both the page and its updates, so there's no second renderer in JavaScript to keep in step. If the app is down it keeps the last content and shows a small "Last updated 14:02" in faint text, instead of an error page.
- Errors (no token, a bad one, a write token in the URL) are a **plain HTML page** with the reason, not JSON: whatever shows a display page is a screen with nobody to read JSON. A signed-in admin can open the page without a token, to preview it.
- A display page sends `Referrer-Policy: no-referrer`, `Cache-Control: no-store` and `X-Robots-Tag: noindex`, since its token is in its own URLs.
- The shell is `templates/display/base.html` (section 12): plain CSS and design tokens, so it works with `uniform.css` and with the Tailwind entry. The app's template extends it and fills `body`.
- The same data as the JSON, so anything a display page shows is also reachable as JSON for screens that render their own (TRMNL's Liquid, the ESP32s).
- Vital Signs can show any display page as a normal web page, and can list them from `meta` to offer them as content.

## 10. Caching and politeness

- Every GET answers with an `ETag`. A reader sending `If-None-Match` gets `304` with no body. Polling every 30 seconds then costs almost nothing.
- `Cache-Control: private, max-age=<refresh_s / 2>` on status and feeds.
- **Every answer carries a `Cache-Control`, and it's `private` or `no-store`, never `public` and never missing.** Everything under `/api/v1/` and `/display/` needs a token, and a shared cache (the tunnel's Cloudflare) stores what isn't marked and serves it to the next reader, token or not. `du_api` sets it on what it answers (status, feeds, images, display pages, errors); the app's own `after_request` makes every other answer `no-store` (`APP-STANDARD.md` 12). `scripts/check_api.py` checks status, a feed and an error for it.
- Answers aim to be small: status under 2 KB, a feed page under 50 KB. The ESP32s parse JSON in a few hundred KB of RAM.
- An app that's busy (an import) still answers status quickly from what it has, with `state: "busy"`, rather than blocking.
- Per-token limit: 120 requests a minute, then `429` with `Retry-After`. Images and events don't count towards it.
- The rate limit and the bad-token lockout live in memory, per gunicorn worker. With two workers they're about twice as loose. That's accepted, since tokens are 256-bit, and an app says so in its `docs/API.md` rather than adding shared state.

## 11. Versioning

- `api: 1` in `meta`. Within v1, changes are **additive only**: new feeds, new fields, new actions, new event types. Readers ignore fields they don't know.
- A breaking change means `/api/v2/` alongside v1 for at least a month, with v1 marked `"deprecated": "2027-01-01"` in `meta`.
- Each app documents its endpoints in `docs/API.md`: every feed with its `meta` fields, every action with its body, every event type, and every display. That file is what Claude reads before writing a reader.

## 12. Shared code

Lifted from Binge Stats' pilot (decided) and released in v1.10. An app takes these through the design updater, like `du_flask.py`, and never edits them in place.

**`tools/du_api.py`**, a Flask module that imports nothing from any app:

- `init_api(app, *, name, title, version, sha, accent, icon, time_zone, check_token, is_admin_session, csrf_ok, legacy_error=None, max_streams=8)` installs the gate on every request under `/api/v1/` and `/display/`: who's asking (Bearer, `?token=` for read tokens, or the admin session), the URL-token rule, the lockout and rate limit, `API_PUBLIC`, and weak ETags with `304`. It returns the core (also `app.extensions["du_api"]`). `check_token(token)` returns `{id, name, scope}` or `None`; `sha`, `time_zone`, `is_admin_session` and `csrf_ok` are callables. An app that already had `/api/v1` routes passes `legacy_error(code, message, status)` so refusals on those keep their old shape.
- The registry behind `meta`: `core.add_feed(name, title, refresh_s, params)`, `add_action(name, title, body, when=None)` (`when` hides an action that's switched off), `add_display(name, title, sizes)` and `add_events(types)`; `core.meta()` builds the answer. `core.open_endpoints` (no token, e.g. a health check) and `core.unmetered_endpoints` (images and events) are sets of endpoint names.
- `@standard("read" | "write")` on each of the standard's routes: scope check, CSRF for a session's writes, the standard's error shape. `identity()` says who's asking, for older routes and for the action log. `ApiCore.error(code, message, status)` builds an error; `cached(resp, refresh_s)` sets `Cache-Control`.
- `@display` on each display page: read access, the HTML error page, and the no-referrer, no-store headers.
- `image_response(cache_dir, kind, ident, fetch)`: `fetch(box)` returns the original bytes, fetched with the app's own credentials, at least `box` wide; the module snaps the width, resizes with Pillow, picks WebP or JPEG, honours `fmt`, `q` and `gray`, caches on disk and sends an ETag. `prune_image_cache(dir)` for the app's background job.
- `event_stream(rows_after, last_id, stopping=core.stopping)`, `StreamSlots` (`core.streams.take()` gives a release function or `None` when full) and `stop_streams_on_sigterm(core)`, for `/api/v1/events`. `rows_after(id)` reads the app's own `events` table (section 8).
- `sign(body, secret)`, `verify(body, secret, header)` and `RETRY_AFTER_S`, for webhooks. Each app keeps its own `webhooks` and `webhook_deliveries` tables and sends from its background worker.
- `iso(ts)` and `parse_iso(s)` for the standard's UTC times.

Pillow is the one new dependency (images only).

**Added in v1.11, for draft 4.** None of it changes what an app already passes in, and `tools/check_du_api.py` checks it all, including that an app passing nothing new is unchanged:
- `check_token(token)` may also return `app` (a short name, or `None`). Missing means `None`, so an app that returns `{id, name, scope}` is unchanged. `identity().app` says which app is asking.
- `@standard("app")`: an app token only (a write token with an `app`), for `/api/v1/inbound/` routes. Anything else gets `403` with the sentence from section 3.
- `core.add_inbound(kind, fields)` puts `inbound` in `meta`. `valid_key(key)` checks a key, and `reopens(status, old_due, new_due)` applies the "a later due date reopens it" rule.
- `add_action(..., limit="10/hour")` shows the limit in `meta`. The action's route calls `core.over_limit(name)`, which returns the `429` (with `Retry-After` and the time it can go) or `None`.
- `local_time(s, field_name)` reads a wall-clock time sent in, giving `("2026-12-24", "09:00" | None)` or a `ValueError` with the sentence. `local_out(day, at, tz)` gives `{"due", "due_at"}` for an answer.
- `webhook_matches(event_types, source_app, event)` applies a webhook's event-type and "Only items from" filters.

**Templates:**
- `templates/display/base.html`: the display page shell (section 9).
- `templates/partials/api_tokens_card.html`: tokens (made, listed, revoked), previews of the app's display pages, and the latest API actions. Set `api_tokens_apps = True` to add a **Belongs to an app** field, and pass `webhook_sources` to the webhooks card to add **Only items from** (draft 4). `templates/partials/webhooks_card.html`: webhooks with their last success or error, the signing secret shown once, and the event-type filter. Both go in Settings after the app's own settings, before the Design system card; each documents the variables it needs at its top. Revoke, New secret and Delete use `js/confirm.js` (DESIGN-SPEC 7.10) when the page loads it.

**`tools/check_api.py <base_url> <read_token> [write_token] [app_token]`** checks a running app against this standard: `meta` and `status` shapes, every feed's item shape, ETag and `304`, a read token in the URL, a bad token, a write token in the URL refused, images in WebP and JPEG, the events stream, and each display page with and without a token. Standard library only, so it runs on the app's host. Read-only, unless it's given an app token: then it also runs one inbound item through PUT, PUT again, GET, done, a reopening PUT and DELETE, on a throwaway `check-api-…` key, which it leaves archived. Run it after each app's change, like `check_du_sync.py`.

**In an app's updater config** (DESIGN-SPEC 18.1), the lines for these, with the app's own paths:

```toml
"tools/du_api.py" = "<package>/du_api.py"
"templates/display/base.html" = "<package>/templates/display/base.html"
"templates/partials/api_tokens_card.html" = "<package>/templates/partials/api_tokens_card.html"
"templates/partials/webhooks_card.html" = "<package>/templates/partials/webhooks_card.html"
"tools/check_api.py" = "scripts/check_api.py"
"API-STANDARD.md" = "docs/design/API-STANDARD.md"
```

## 13. Readers

How each reader uses it, once apps have it:

- **Docket:** Settings → **App feeds** holds each app's base URL and a read token (stored like the mail settings, out of the database and backups). A new MCP tool `feed_fetch(app, feed, limit?, since?)` and `app_status(app)` read them server-side, so briefs stop needing claude-ops recipes and Claude never handles a token. This is build step 8.
- **Vital Signs:** a source type `du_app` (base URL and token in `server.toml`) that the data proxy understands, plus "Display page" as content, picked from the app's `meta`.
- **PC panel agent:** reads `status` and feeds with a read token (already does, against Binge's v1), and calls actions for the panel buttons with a separate write token.
- **Home Assistant:** a `rest` sensor per app on `/api/v1/status` (`value_template: "{{ value_json.state }}"`, `json_attributes: [headline, counts]`), `rest_command`s for actions, and webhook triggers for events.
- **TRMNL:** a private plugin polling `https://<app>.marctew.co.uk/api/v1/status?token=…` (and a feed if needed), with a Liquid template. Only for apps on the tunnel.

## 14. Rolling it out

One app at a time, like the design system. **Binge Stats goes first, as the pilot (decided 5 Nov 2026):** the shared code comes out of it rather than going into it.

1. **Done, 5–6 Nov 2026: Binge Stats built the standard in-app,** to its own plan (`docs/specs/api-standard.md` in its repo), in six stages: the shared core with `meta` and `status`, then feeds and images, actions, events and webhooks, the `now-playing` display page, and docs with checking. Its current endpoints stayed exactly as they were (section 1). What was checked against the live server, including a 5½-minute events stream through the Cloudflare tunnel, is in its `docs/verification.md`.
2. **Done in v1.10: the shared code lifted** into this repo (`du_api.py`, the API tokens and webhooks cards, the display shell, `check_api.py`), with what the pilot found written in (section 17). Binge takes the shared copies through the design updater.
3. **Docket:** `status` (due today, overdue, the night shift), feeds (`today`, `upcoming`, `reports`), actions (`add_task`, `complete_task`, and `notify` limited to 10 an hour), **inbound items** (tasks with a reminder, for Muddy Paws first), events (`task.due`, `task.done`, `task.reopened`, `brief.published`, `watch.fired`, `approval.requested`). Then its reader side (section 13), which is step 8. Muddy Paws' own brief for this (`docket-inbound-api.md`) maps onto draft 4 as: its `/api/inbound/items/<key>` routes become `/api/v1/inbound/items/<key>`, its `notify` becomes the `notify` action, its Connected apps card becomes app tokens on the API tokens card, and its Done callback becomes a webhook filtered to Muddy Paws' items.
4. **Done, 6 Nov 2026: Encore** (downloads): status; feeds `downloads`, `arrivals`, `failed` and `wanted`; cover and artist images; actions `want_album`, `get_album` (30 an hour), `retry_job` and `search_wanted` (4 an hour); events `job.queued`, `download.started`, `download.finished`, `download.failed` and `wanted.added`; webhooks; the `downloads` display page. Documented in its repo's `docs/API.md`, checked by its `tools/check_api_encore.py` and by `check_api.py` on the LAN and through the tunnel. What it found is in section 20.
5. **Next:** **PodBridge** (new episodes, sync state), **Vital Signs** (its own status), **The Back Room** (status only, LAN only, unless Marc wants more).
6. claude-ops is deployed only by Marc and keeps its own API; it isn't part of this unless he says so.

## 15. Open questions

1. **TRMNL:** does Marc have a TRMNL device to target, or is it only the builder for other people? It changes whether display pages need an 800x480 one-bit mode (`static=1&gray=1`) first.
2. **Which display pages first?** Likely candidates: Binge "now playing", Docket "today", Encore "downloads".
3. **HA discovery:** should apps also offer an MQTT discovery option (Stagehand already talks MQTT to HA), or is REST plus webhooks enough?

## 16. Changes in draft 2

From Binge Stats' pilot plan (5 Nov 2026), all accepted:

1. **Events through SQLite** with the row id as `Last-Event-ID`, not an in-memory ring. This works with more than one gunicorn worker and with a separate worker process. The stream cap is 8 per app, split across workers. (Section 8.)
2. **Existing `/api/v1/` routes keep their error shape.** Only the standard's routes must use `{"error", "code"}`. (Sections 1 and 5.)
3. **Only an admin session** reads `/api/v1/` without a token, in apps with viewer or non-admin logins. (Section 3.)
4. **The access-log format** that keeps tokens out of logs, and `Referrer-Policy: no-referrer` on display pages. (Section 3, and `APP-STANDARD.md` 3.)
5. **Image cache path** under `/var/cache/<app>/` (matching `APP-STANDARD.md`), or the data volume for Docker apps. (Section 6.)
6. **In-memory limits per worker are accepted** and documented, with no shared state. (Section 10.)

Also: Binge is the pilot and the shared code is lifted from it (sections 12 and 14), and webhook delivery always runs in a background job, with a retry schedule of 1, 3 and 6 minutes (section 8).

## 17. Changes in draft 3

From building and running the pilot in Binge Stats (6 Nov 2026):

1. **The lockout is keyed by `CF-Connecting-IP`** when it's there, so one stranger can't lock out every reader coming through the tunnel. (Section 3.)
2. **A repeat answered from an `Idempotency-Key` isn't logged again**, and a `502` isn't remembered against its key. An admin session can call actions too, with CSRF. (Section 7.)
3. **Display pages reload their own body** (`fragment=1`) rather than re-rendering JSON in the browser, **refuse with a plain HTML page**, and send no-referrer and no-store. (Section 9.)
4. **Section 12 now describes the shared code as built**: `init_api` with callables and an optional `legacy_error`, `@standard` and `@display` rather than per-feed decorators, `image_response`, `event_stream` with `StreamSlots`, and `sign`. Each app keeps its own events and webhook tables. The updater config lines are listed.
5. **Docs:** the tokens and webhooks cards say what they need; `tools/check_api.py` also checks images, the events stream and display pages.
6. **`since` and `cursor` are only for feeds ordered by time**, newest first. A live queue or a schedule ignores them and answers whole up to `limit`, as Binge's `now_playing`, `downloads` and pins feeds do. (Section 5, v1.10.1.)

## 18. Changes in draft 4

For Muddy Paws keeping reminders in Docket (6 Nov 2026), and any app that needs to keep things in another:

1. **Inbound items** at `/api/v1/inbound/items/<key>`: create-or-update by the sender's own key, read, done and archive, with a later due date reopening a done item. (Section 7.) Not `/api/v1/items/`, because Binge Stats already has an older route there (`/api/v1/items/<rating_key>`).
2. **App tokens:** a write token can belong to an app, and only that app's token can see or change its inbound items. (Section 3.)
3. **Times sent to an app:** wall-clock fields are local (`YYYY-MM-DD HH:MM` or a date), moments are UTC. (Section 5.)
4. **Per-action limits** in `meta`, such as `notify` at 10 an hour. (Section 7.)
5. **Events about inbound items carry their `source`**, and webhooks can be filtered to one app's items, which replaces a bespoke "callback URL". (Section 8.)
6. **Shared code to add in v1.11.** Every addition is optional for an app that doesn't use it. (Section 12.)

## 19. What draft 4 means for Binge Stats

Binge runs draft 3 on the v1.10 shared code. **It doesn't have to change anything for draft 4.** Every addition is optional, and an app that doesn't take part in one is left exactly as it is. In detail:

| Draft 4 change | Does Binge need to change? |
|---|---|
| Inbound items | **No.** Binge doesn't take inbound items, and it doesn't list `inbound` in `meta`. The new routes live under `/api/v1/inbound/`, which Binge has never used. **One rule for Binge from now on:** never add an older-style route under `/api/v1/inbound/`. |
| App tokens | **No.** `check_token` can keep returning `{id, name, scope}`, because a missing `app` means none. Binge's `api_tokens` table needs no new column. The tokens card hides the **Belongs to an app** field unless `api_tokens_apps` is set. |
| Times sent to an app | **No.** None of Binge's actions (`pin_show`, `unpin_show`, `ack_alert`, `ack_all_alerts`, `move_download`) take a time. If one ever does, it follows section 5. |
| Per-action limits | **No.** They're optional. Binge's actions are fine on the token's 120 a minute. |
| Events with `source` | **No.** Only events about inbound items carry it, so Binge's `events` table needs no `source_app` column. The webhooks card hides **Only items from** unless `webhook_sources` is passed. |
| v1.11 shared code | **Check, don't change.** When the updater brings v1.11 in, `du_api.py`, both cards and `check_api.py` are replaced. Run Binge's test suite and `scripts/check_api.py` against it once it's live, the same as after v1.10. Nothing in Binge's `init_api(...)` call needs to change. |
| Docs | **A one-line fix.** `docs/API.md` still says "draft 2" at the top. Make it "draft 4". |

## 20. What Encore found (6 Nov 2026)

Encore is the first app to take the shared code (v1.10 to v1.14) without having been the pilot. All of it fitted; these are what the next apps should know:

1. **Cloudflare's Browser Integrity Check blocks the API through the tunnel** (`403`, `error code: 1010`) until the app's hostname has the Configuration Rule in section 4. Binge Stats was blocked the same way and has the rule too.
2. **Header names arrive in lower case through Cloudflare**, which made `check_api.py` (before v1.15) report a missing `ETag` and `Referrer-Policy` that were there. Readers look headers up case-insensitively.
3. **The API tokens and Webhooks cards are plain HTML forms.** An app whose CSRF defence is a custom header on its own page's requests (Encore's `X-Encore: 1`) can't take those posts as they are: give it a real `csrf_token()`, tied to the session (Encore uses an HMAC of the session cookie with a key in its data folder), and accept it as the cards' `csrf_token` form field and as `X-CSRF-Token` (the Design system card). A fixed value there would be no defence at all.
4. **The login check must step aside for `/api/v1/` and `/display/`**, or token readers get the app's own `401`. `du_api` checks those paths itself; nothing else may skip the login.
5. **Feeds that should outlive the app's own lists need a table of their own.** Encore's queue can be cleared, so `arrivals` comes from a table its queue writes to whenever a job is filed, not from the queue.
6. **Events from a background thread** (Encore's queue) need the app's public address for item `url`s. Encore remembers the address from real requests, since a thread has no request of its own.

