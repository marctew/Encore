# Encore's API

Encore follows `docs/design/API-STANDARD.md` (draft 4): read that for the shapes, tokens, errors, ETags, images, events and display pages every app shares. This file is what's Encore's own: its status, feeds, actions, events and display page.

- **Base:** `http://<encore>:3346/api/v1/` on the LAN, and `https://<encore's tunnel hostname>/api/v1/` through the Cloudflare tunnel. Both always need a token. Set `API_PUBLIC=0` in the service to refuse the tunnel.
- **Tokens:** Settings → **API tokens**. Read tokens may go in the URL as `?token=`; write tokens only as `Authorization: Bearer <token>`. They start `enc_`.
- **Time zone:** `Europe/London` (set `ENCORE_TIME_ZONE` to change). `at` is UTC `...Z`; `at_label` is the same moment in that zone ("Today 19:12", "Yesterday 08:01", "Sat 7 Nov").
- **Limits:** 120 requests a minute per token, plus the per-action limits below. In memory, one gunicorn worker.
- **Not here:** Encore's own page API (`/api/...` without `v1`) is for its own page only, with the session cookie and `X-Encore: 1`.

Checked by `tools/check_api_encore.py` (against fakes) and `scripts/check_api.py <base> <read token> [write token]` (against a running Encore).

## Status: `GET /api/v1/status`

| Field | Encore's meaning |
|---|---|
| `state` | `error` when the folders aren't set; `busy` while anything is downloading, being checked, filed or scanned; `warning` when a download has failed (and is still in the queue), an album from one YouTube video is waiting to be checked, or SABnzbd isn't set up; otherwise `ok` |
| `headline` | "2 albums downloading", "1 download failed", "1 album to check", "Nothing downloading" |
| `detail` | The album being worked on ("Now: Fleetwood Mac – Rumours, Downloading 64%"), the failure's reason, or what's next |
| `counts` | `downloading`, `queued`, `failed`, `to_check` (albums from one YouTube video waiting for their tracks to be checked), `arrived_today` (since local midnight), `wanted` |

`refresh_s` is 30.

## Feeds: `GET /api/v1/feeds/<name>`

Every item: `id`, `title` ("Artist – Album"), `subtitle`, `at`, `at_label`, `tone`, `image` (`/api/v1/images/cover/<album id>`, `/api/v1/images/artist/<artist id>` for a music video, or null for an album MusicBrainz doesn't have), `url` (Encore's page), and `meta` below.

| Feed | Order | Parameters | Items |
|---|---|---|---|
| `downloads` | The queue's: running first, then those waiting to be checked, then waiting in queue order. Answered whole up to `limit`; ignores `since` and `cursor` | `limit` | `subtitle` is the job's progress ("Downloading 64%", "SABnzbd: Extracting", "YouTube track 4 of 12 · 30%", "Filing: copying 3 of 12 files · 40%", "Waiting"). `tone` `info` running, `warning` waiting to be checked, `muted` waiting. `at` is when it was queued |
| `arrivals` | Newest first, by when it was filed | `limit`, `since`, `cursor` | Everything Encore has filed, kept after **Clear finished**. `subtitle` "In your library · 24/96", "In MusicImport", "Music video". `tone` `success`, or `warning` for MusicImport |
| `failed` | Newest first. Answered whole up to `limit` | `limit` | Failed jobs still in the queue. `subtitle` is the reason. `tone` `danger` |
| `wanted` | Newest first, by when it was added | `limit`, `since`, `cursor` | The Wanted list. `subtitle` "1977 · Album". `tone` `accent` |

`meta` on job items (`downloads`, `failed`): `job_id`, `album_id`, `artist`, `album`, `kind` (`album`, `youtube`, `youtube_album`, `youtube_list`, `video` or `single`), `state` (`queued`, `downloading`, `verifying`, `filing`, `scanning`, `review`, `failed`), `percent`, `quality`.
On `arrivals`: `job_id`, `album_id`, `artist`, `album`, `kind`, `where` (`library` or `import`), `folder`, `quality`, `message`.
On `wanted`: `album_id`, `artist_id`, `artist`, `album`, `year`.

`album_id` is the MusicBrainz release group id throughout.

## Images: `GET /api/v1/images/<kind>/<id>`

- `cover/<album id>`: the album's front cover from the Cover Art Archive.
- `artist/<artist id>`: the artist's photo, from Plex or the one Encore keeps.

`w`/`h` snap to 80, 160, 320, 640, 1280; WebP when accepted, else JPEG; `fmt=jpeg`, `q=`, `gray=1` as in the standard. Cached in `/var/cache/encore/images` (500 MB, oldest out first).

## Actions: `POST /api/v1/actions/<name>`

Write token in the header. Send a JSON object; an `Idempotency-Key` header makes a retry safe for 24 hours. Every answer that ran is `{"ok": true, "message": "...", "result": {...}}`. Every action, refused or not, is in Settings → **API tokens** → Recent API actions, with the token's name.

| Action | Body | Limit | Does |
|---|---|---|---|
| `want_album` | `{"album_id": "<mbid>"}` | – | Adds the album to Wanted. Nothing downloads. `result`: `{album_id, added}` (`false` if it was already there) |
| `get_album` | `{"album_id": "<mbid>"}` | 30 an hour | Searches the indexers and queues the release the page would mark **Best match** (rated good, not failed before). If nothing rates good, the album goes on Wanted instead. If it's already queued or owned, nothing happens and the message says so. `result`: `{album_id, queued}` |
| `retry_job` | `{"job_id": "..."}` | – | Retries a failed job, like its Retry button. `job_id` is in the `downloads` and `failed` feeds |
| `search_wanted` | `{}` | 4 an hour | Searches for every album on Wanted, one at a time in the background, and queues each one that has a good release. Answers straight away with how many it's searching. One search at a time: a second one while it runs gets `503 busy` |

Refusals use the standard's `{"error", "code"}`: `bad_request` for a bad `album_id`, or when Encore's indexers, SABnzbd or folders aren't set up; `not_found` for an unknown action or job; `upstream` (502) when MusicBrainz or the indexers didn't answer, which isn't remembered against the `Idempotency-Key`, so retrying it can work.

## Events: `GET /api/v1/events`

Server-sent events, read token. Replays up to 200 after `Last-Event-ID` (or `?last_event_id=`); a `: ping` every 25 seconds. At most 8 open streams. Each `item` is in the feed item shape above.

| Type | When |
|---|---|
| `job.queued` | An album or video is added to the queue, or a failed one is retried |
| `download.started` | A queued job starts downloading |
| `download.finished` | A job is filed: in the library or MusicImport (`meta` doesn't say which here; `title` does, and the `arrivals` feed has `where`) |
| `download.failed` | A job fails; `subtitle` is the reason |
| `wanted.added` | An album goes on Wanted: from the page, `want_album`, or `get_album` finding nothing good |

## Webhooks

Settings → **Webhooks**: each event above is POSTed as JSON (the stream's `data`), signed `X-Signature: sha256=<HMAC-SHA256 of the body>` with the webhook's secret, retried after 1, 3 and 6 minutes.

## Display page: `GET /display/downloads`

What's downloading (cover, progress), up next, and the last four arrivals. Sizes `800x480`, `1280x720`, `1920x1080`; the page fills whatever it's given. Takes `token`, `theme=light`, `scale`, `accent` (Encore's is `blue`), `static=1`. Reloads its body every 15 seconds and straight away on a queue event. Preview it from Settings → **API tokens**.

## Home Assistant

```yaml
rest:
  - resource: http://encore.lan:3346/api/v1/status
    headers: {Authorization: "Bearer enc_…"}
    scan_interval: 30
    sensor:
      - name: Encore
        value_template: "{{ value_json.state }}"
        json_attributes: [headline, detail, counts]
rest_command:
  encore_get_album:
    url: http://encore.lan:3346/api/v1/actions/get_album
    method: POST
    headers: {Authorization: "Bearer enc_…"}
    content_type: application/json
    payload: '{"album_id": "{{ album_id }}"}'
```
