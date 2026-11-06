# Encore

Finds albums on Usenet, checks every track, and files them where Plex can see them. Artists, albums and track lists come from MusicBrainz, releases from Newznab indexers, downloads through SABnzbd. Read README.md before changing behaviour.

Built to `docs/design/APP-STANDARD.md` and styled to `docs/design/DESIGN-SPEC.md` (from `C:/ClaudeCowork/Design Uniformity`). Follow both; no one-off styles or layouts. Encore is Downloads category (`accent-blue`). Its own styles are in `encore/static/encore.css`; `encore/static/design/`, `encore/templates/macros/`, `encore/templates/partials/`, `encore/du_flask.py`, `encore/du_api.py`, `encore/templates/display/base.html`, `scripts/check_api.py` and `docs/design/` are copied in by the design updater, so never edit them here.

## Layout

- `encore/` the Flask package. `web.py` has the app factory (`create_app`), the routes and the guard; `engine.py` the queue and filing; `sources.py` the MusicBrainz, indexer, SABnzbd and Deezer clients; `youtube.py` yt-dlp, run as its own program; `api.py` the outside API (`/api/v1/`, `/display/`, documented in `docs/API.md`), on the shared `du_api.py`; `plex.py`, `store.py` (SQLite), `match.py`, `filing.py`, `auth.py`.
- `encore/templates/` the pages; `encore/static/` the page scripts: `shell.js` (shared), `encore.js` (Find, Library, Wanted, Queue and artist pages as views of one page, each with its own URL) and `settings.js`.
- `wsgi.py` serves it under gunicorn (one worker: the queue runs in-process); `python -m encore` runs it alone for development.
- `scripts/update.sh` deploys a checkout; `install.sh` and `deploy/encore.service` set up a new machine.

## Rules

- Data lives in `/var/lib/encore` (`ENCORE_DATA`): settings, the queue, the database, photos, the password. Never in the repo.
- Every non-GET request needs the header `X-Encore: 1`. That header is the CSRF defence; keep it. The shared Settings cards send `csrf_token()` instead (the Design system card as `X-CSRF-Token`, the API tokens and Webhooks cards as a form field), which is tied to the session.
- `/api/v1/` and `/display/` are the outside API: `du_api` checks their tokens, not the guard. Every route there is marked `@standard` or `@display`, and nothing else may skip the login. Tokens and webhook secrets are shown once; only token hashes are stored.
- With a password set, everything except `/login`, `/api/login`, `/healthz`, `/favicon.ico` and `/static/` needs the session cookie. If Encore is reachable from the internet, the password is its only protection: never remove or weaken it.
- Saved API keys never go back to the page. A blank key field keeps the saved one.
- Restart with `sudo scripts/update.sh` (`--pull` to pull first). It refuses while an album is being checked or filed; `--if-idle` leaves the new code for the next restart instead.
- This is a public repo: keep personal addresses, paths and keys out of tracked files, and scan staged changes before pushing.

## Tests

`tools/check_usenet.py` (indexers, SABnzbd and the queue against fakes), `tools/check_youtube.py` (YouTube albums against a fake yt-dlp, `tools/fake_ytdlp.py`), `tools/check_api_encore.py` (the outside API against fakes) and `tools/check_auth.py` (the password, the guard, and matching Plex artists) print PASS lines and exit non-zero on a failure. Run all four with the venv's Python after any server change. Try page changes on a throwaway copy of the data on a spare port, never on the live queue.
