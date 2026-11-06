# App standard

How Marc's homelab apps are built, so every app is put together, run and updated the same way. `DESIGN-SPEC.md` covers how they look; this covers everything underneath.

**The reference app is The Back Room** (`marctew/backroom`, LXC 455). When this file and an app disagree, this file wins; when this file is silent, copy The Back Room.

## 1. The stack

| | Standard | Why |
|---|---|---|
| Language | Python 3 from the distro, with packages from `apt` where it has them | One toolchain to keep up to date. When an app needs packages `apt` lacks (or too old), it uses `.venv/` in the checkout, filled from `requirements.txt` by `scripts/update.sh`, as Vital Signs does. |
| Web | Flask, Jinja templates rendered on the server | Every app is Flask, except claude-ops (below). |
| Server | gunicorn, **one worker**, many threads (`--threads 32`) | Apps keep jobs and caches in memory, so there must be one process. Threads let video streams and slow requests run side by side. |
| Process | a systemd service, one app per LXC | Restarts, logs and start-up order come free. |
| CSS | `static/design/uniform.css` plus the app's own `static/<app>.css` | No build step, so a design update is just files. |
| JS | Plain JavaScript files in `static/`; no framework, no bundler | Same reason. |
| Data | SQLite or JSON files in `/var/lib/<app>/` | No database server to run. |

**The exception is Binge Stats**, which other people install from GHCR. It stays in Docker and on Tailwind (`css/tailwind-components.css` maps Tailwind onto the same design), but the rest of this file still applies: `/healthz`, `scripts/update.sh`, the Settings card and the repo layout.

**claude-ops is the other exception.** It's the MCP server Claude operates the homelab through, so its security model comes first. Its MCP endpoint must stay on FastMCP, and its admin page is a small Starlette app in the same process, sharing its state. Both stay in Docker on LXC 995, which Claude can't reach; Marc reviews and deploys every change. Its page takes the look (DESIGN-SPEC 7.15 included) but none of the machinery: the shared CSS is copied in by hand and inlined into each page, so its CSP (inline styles and scripts only, nothing loaded from anywhere) never changes. There's no updater, no Design system card and no `scripts/update.sh`.

**Not allowed:** the Flask development server in production; more than one gunicorn worker; Node or a CSS build step (except Binge); CSS or JS written inside Python strings.

## 2. Layout

```
<repo>/
  <app>/                 the package: python3 -m <app> runs a development server
    __init__.py          imports every module (so their routes register), defines start_workers();
                         its docstring lists every setting
    web.py               app = Flask(__name__), secret key, session settings, template filters
    config.py            every setting, read from the environment
    health.py            /healthz
    auth.py              the login check and the login, logout and password routes
    <area>.py            one module per area of the app, each adding its own routes
    du_flask.py          copied in by the design updater; never edit it here
    templates/           base.html plus one .html per page
    static/
      <app>.css          the app's own styles, on top of the design system
      <app>.js           script every page needs
      design/            copied in by the design updater; never edit it here
  wsgi.py                what gunicorn runs: imports app, calls start_workers()
  tools/                 command-line scripts the app runs or you run by hand
  scripts/update.sh      deploys this checkout (section 6)
  deploy/<service>.service           the systemd unit
  deploy/<service>.env.example       the template for the secrets file
  docs/design/           DESIGN-SPEC.md, this file and PWA-ICONS.md, copied in by the design updater
  README.md              what it is, a layout table, how to run it, what isn't in the repo
```

- **Modules import downwards** (`config` → `web` → shared logic → pages) and never in a loop. A module that needs something from a "higher" one means that something belongs lower.
- **One way to make the app per app.** Either one module-level `app` with plain endpoint names (The Back Room), or a `create_app()` factory with a blueprint per module (Vital Signs, whose tests build a fresh app each time). Don't convert one to the other.
- **A repo with more than one program** names each package after its program (Vital Signs has `server/` and `agent/`), and `wsgi.py` serves the web one.
- **Templates are `.html` files**, so Jinja autoescapes them. Never `render_template_string` or a `DictLoader`.

## 3. Running

The unit, from The Back Room's `deploy/hmm-browser.service`:

```ini
[Unit]
Description=The Back Room media browser
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
Environment=HMM_PORT=8765                 # ordinary settings here
Environment=PYTHONUNBUFFERED=1            # print() reaches the journal straight away
EnvironmentFile=/etc/hmm-browser.env      # secrets: chmod 600 root
WorkingDirectory=/opt/hmm-browser
ExecStart=/usr/bin/gunicorn --workers 1 --threads 32 --bind 0.0.0.0:8765 --timeout 120 --graceful-timeout 20 --access-logfile - wsgi:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

| What | Where |
|---|---|
| Code (the git checkout) | `/opt/<app>` |
| Secrets | `/etc/<app>.env`, `chmod 600 root`, or a config file in `/etc/<app>/` when settings are richer than a list of variables (Vital Signs' `server.toml`). Never in the repo, never in the unit, never in a page. Secrets an admin enters in Settings follow section 11. |
| Data | `/var/lib/<app>/` |
| Caches | `/var/cache/<app>/` |
| Design updater state | `/var/lib/<app>/design/` |
| Logs | the journal: `journalctl -u <service>` |

**Keep tokens out of the access log.** gunicorn's default access line includes the query string and the referrer, and displays send read tokens as `?token=` (`API-STANDARD.md` 3). Add this to `ExecStart`, so the log has the path without the query string and no referrer:

```ini
--access-logformat '%%(h)s %%(t)s "%%(m)s %%(U)s %%(H)s" %%(s)s %%(b)s "%%(a)s"'
```

Every `%` is doubled because systemd reads `%` as one of its own specifiers. On a command line or in a Docker entrypoint, write it with single `%`s. Push-button links that carry a token in the path (Docket's `/n/<token>`) still show in this format. They're one-use and short-lived, and the journal is root-only.

New apps use one name for the folder, service and env file. Older apps keep the names they have (The Back Room is `hmm-browser`) rather than risk a rename.

**Apps that serve large files with Range requests** (video, audio): gunicorn's `wsgi.file_wrapper` can't seek, so Werkzeug's `send_file` reads and discards everything before the requested range. That made a seek halfway into a 300 MB video on NFS take 20 seconds in The Back Room. Hand `send_file` Werkzeug's own wrapper instead; copy `_seekable_files` from The Back Room's `backroom/web.py`, and time a mid-file Range request before switching an app to gunicorn.

**Background jobs** (rescans, imports, polling) start in `start_workers()`, which `wsgi.py` calls once in the gunicorn worker. Each job keeps its state in a dict with a `"running"` flag, and `health.py` lists those flags.

## 4. What every app has

- **Login:** a session cookie named after the app, `SameSite=Lax` and 90 days long, signed with a key kept in `/var/lib/<app>/`. The password is stored only as a hash and changed in Settings. Changing it logs out every other session. Every request is checked in one `before_request`, which lets through only `login`, `static` and `healthz`. Pages redirect to `/login?next=…`; `/api/*` answers `401 {"error": "Log in again."}`. An app where each person signs in as themselves follows section 9; endpoints that devices post to follow section 10.
- **`/healthz`:** no login, and it says nothing about the app's contents: `{"ok": true, "busy": ["import"]}`. `busy` names any job a restart now would cut short.
- **Settings page:** the app's own settings, then the password, then the **Design system** card (`DESIGN-SPEC.md` 7.14) last.
- **A version and an About page** (`DESIGN-SPEC.md` 7.15): `__version__` in the package, passed to `init_design(..., version=__version__)`, the shared sidebar footer (Sign out, theme switch, `v1.0.0 · sha · About`) and `/about`.
- **JSON APIs:** actions are `POST` with a JSON body, answering JSON. Errors are `{"error": "…"}` with a 4xx status, written for a person to read. These are the app's own page APIs. What other things read (displays, Docket, Home Assistant) is the public `/api/v1/` in **`API-STANDARD.md`**.
- **Feedback:** toasts (`toast.js`) for results, inline confirm popovers (`DESIGN-SPEC.md` 7.10) for anything destructive. Never `alert()` or `window.confirm()`.
- **Behind a proxy:** `ProxyFix(app.wsgi_app, x_proto=1, x_host=1)`, so links are right through h2s or Tailscale.

## 5. Git

- Each app is its own repo on GitHub under `marctew`. It's private unless other people are meant to install it.
- `/opt/<app>` is a checkout of it, and it stays clean: every change is committed, because the design updater refuses to run on uncommitted changes.
- Commit messages say what changed and why, in plain English. The design updater commits as "Design Uniformity".
- `.gitignore` has at least `__pycache__/`, `*.pyc`, `*.bak*` and `*.env`, plus `!deploy/*.env.example`.

## 6. Deploying: `scripts/update.sh`

Every app has one, run as root on its host. It:
1. with `--pull`, does `git pull --ff-only` first;
2. compiles the package, and stops without restarting if that fails;
3. asks `/healthz` whether anything is busy. If it is, it refuses, unless `--force` is given; with `--if-idle` it leaves the new code for the next restart and still succeeds;
4. restarts the service and waits up to a minute for `/healthz`. If that doesn't come, it prints the last of the journal and fails.

Exit codes: 0 means deployed (or deliberately left for later with `--if-idle`), 1 means something is wrong, 2 means a bad option.

Read `/healthz` with Python's `urllib`, not `curl`: not every LXC has `curl` (Vital Signs' doesn't).

## 7. The design updater

`/etc/design-uniformity/<app>.toml` for a standard app (`DESIGN-SPEC.md` 18.1 has every option):

```toml
app = "The Back Room"
repo = "/opt/hmm-browser"
state_dir = "/var/lib/hmm-browser/design"
channel = "stable"
deploy = "scripts/update.sh --if-idle"     # never cuts an import short
health = "http://127.0.0.1:8765/healthz"

[files]
"css/uniform.css" = "backroom/static/design/uniform.css"
"css/accents.css" = "backroom/static/design/accents.css"
"glyphs/icons.svg" = "backroom/static/design/icons.svg"
"js/design-card.js" = "backroom/static/design/design-card.js"
"js/toast.js" = "backroom/static/design/toast.js"
"tools/du_flask.py" = "backroom/du_flask.py"
"DESIGN-SPEC.md" = "docs/design/DESIGN-SPEC.md"
"APP-STANDARD.md" = "docs/design/APP-STANDARD.md"
"PWA-ICONS.md" = "docs/design/PWA-ICONS.md"
```

In `web.py`: `from .du_flask import init_design` and `init_design(app, os.environ.get("<APP>_DESIGN_STATE", "/var/lib/<app>/design"))`.

## 8. Bringing an app up to the standard

One app at a time, each in its own commit:
1. Commit or discard anything uncommitted in `/opt/<app>`.
2. Restructure into the layout in section 2. **Move code, don't rewrite it.** Then prove nothing changed: load the old and new apps side by side against a copy of the data and compare their routes and the HTML of every page. The Back Room's split matched byte for byte.
3. Add `/healthz`, `wsgi.py`, `scripts/update.sh` and the gunicorn unit, and test them with gunicorn on a spare port against copied data.
4. Switch over while the app is idle. Keep the old unit file as a backup, and switch back if `/healthz` doesn't come up.
5. Point the design updater at the new paths, and press **Check now** on the card.
6. Add this line to the app's CLAUDE.md:

> Built to `docs/design/APP-STANDARD.md` and styled to `docs/design/DESIGN-SPEC.md`, with home screen icons to `docs/design/PWA-ICONS.md` (from `C:/ClaudeCowork/Design Uniformity`). Follow them; no one-off styles or layouts.

| App | Status |
|---|---|
| The Back Room | Reference app, 2026-11-04 |
| Vital Signs | Done 2026-11-04: gunicorn, `/healthz`, `scripts/update.sh`, restyled. Keeps its venv, TOML config and `server/` + `agent/` layout |
| PodBridge | Done 2026-11-04: Docker to systemd (the Docker leftovers were removed 2026-11-05), restyled, new icon 2026-11-05 |
| Encore | Done 2026-11-04: rewritten from aiohttp to a Flask package, restyled in Downloads blue |
| Binge Stats | Done 2026-11-04: `/healthz`, `scripts/update.sh` and the Settings card; stays in Docker and Tailwind (section 1) |
| claude-ops | Done 2026-11-04: restyled in place, design updates by hand only (section 1) |
| Muddy Paws | Built to the standard 2026-11-05 (v1.0.0), with the extensions in sections 9–11: a login per person, phone ingest with device tokens, and secrets entered in Settings. Uses a venv (section 1) for `aiotractive` and `cryptography` |

Every app above except claude-ops is on the design updater.

## 9. Apps with more than one person

Section 4 assumes one password for the app. When each person needs their own login (Muddy Paws: walks record who came), copy Paws' `paws/auth.py` and `paws/users.py`.

- **A `users` table:** `username` (unique, `COLLATE NOCASE`), `name` (what pages show), `pw_hash`, `role` (`admin` or `member`), `gen`, `created_at`, `pw_changed_at` and `removed_at`. There's no shared password and no password in `/etc/<app>.env`.
- **Roles:** admins add people, reset passwords, change roles and see the app's own settings and secrets; members change only their own name and password. Check with an `admin_required` decorator on each admin route, and refuse to demote or remove the last admin.
- **The session holds `uid` and `gen`.** `current_user()` loads the row once per request and only accepts it while `gen` matches. A password change, a reset by an admin or a removal does `gen = gen + 1`, which signs that person out everywhere; the person changing their own password is signed straight back in, so only their other sessions go. Everyone else stays signed in.
- **First run:** until the first admin exists, every page goes to `/setup`, and `/api/*` answers `503` saying so. `/setup` makes the first admin, and only works from the home network: a request carrying `CF-Connecting-IP` (or `CF-Ray`) came through the tunnel and gets `403`, as does any address that isn't private. Once anyone exists, `/setup` redirects to the login. Check again inside the insert's transaction, so two people can't both finish setup.
- **Removing someone** is soft: set `removed_at`, set `pw_hash` to `'!'`, bump `gen`, and rename the username to `<username>#removed-<id>` so it's free for someone new. Their rows elsewhere keep pointing at them, so history still shows their name. Their device tokens (section 10) stop working in the same transaction. Every query for current people says `removed_at IS NULL`.
- **Lockout, by address and by username:** 5 wrong passwords in 15 minutes from one address, or for one username, locks that key out for 15 minutes, counted in memory (one worker, section 1). A failed login waits a second before answering. The address is `CF-Connecting-IP` when the request came through the tunnel, otherwise the remote address.
- **Passwords:** at least 10 characters when the app can be reached from the internet, with no leading or trailing space. Say why in the error.
- **CSRF:** every `POST`, `PUT`, `PATCH` and `DELETE` carries the session's token, as a `csrf_token` form field or an `X-CSRF-Token` header, checked in the same `before_request` as the login. An expired form flashes "That form had expired, so nothing was changed. Try again."
- **Per-person colours:** each person gets a `colour_slot`, the first chart palette slot nobody current has (`DESIGN-SPEC.md` 11), so they're the same colour on every chart and map.

## 10. Ingest endpoints with their own tokens

For things that post data in without a person at the keyboard: phone tracking apps (Paws' Overland, GPSLogger and OwnTracks), sensors, scripts. Copy `paws/ingest.py` and `paws/devices.py`.

- **Outside the login check.** The ingest routes (`/ingest/<source>`) are in the `before_request`'s open list, so each one checks its own token. Nothing else skips the login.
- **One token per device**, made in Settings: `<prefix>_` plus `secrets.token_urlsafe(24)`. Only its sha256 hash is stored, with the last 4 characters as a hint to tell devices apart. The token is shown once, on the page straight after adding the device or making a new token, with the setup steps for that device's app. A lost token is replaced, never shown again, and making a new one stops the old one at once.
- **In a header, never the URL:** `Authorization: Bearer <token>`, or Basic auth with the token as the password where the device's app only does that (OwnTracks).
- **A known token is never locked out.** At home every phone shares one public address, so one phone with a wrong token mustn't lock the others out. Look the token up first and let a match straight in. Only bad tokens count against the address: 5 in 15 minutes and that address gets `429` for 15 minutes. The tokens are 32 random characters, so the lockout only slows down someone guessing.
- **Errors say what to do:** `401 {"error": "That device token isn't known. Copy it again from Settings in Paws."}`. Answer success in the shape the device's app expects before it drops its batch (Overland needs `{"result": "ok"}`).
- **Safe to resend:** deduplicate on (device, timestamp), and cap a request's size (Paws takes 2,000 points).
- **Removing a device** is soft, as for people: set `removed_at` and overwrite `token_hash` so it can never match. What it sent stays.
- **Nothing reaches the log.** Use the access log format in section 3, which leaves out query strings. Paws logs the `CF-Connecting-IP` header in place of `%(h)s`, since every request through the tunnel comes from cloudflared.

## 11. Secrets an admin enters in Settings

Section 3 keeps secrets in `/etc/<app>.env`. An app may instead let an admin enter another service's login or API key in Settings, so it can be changed without a shell (Paws: the Tractive login and the Google server key). Copy `paws/secretbox.py`.

- **Encrypted at rest:** each value is encrypted with Fernet (the `cryptography` package) and kept in `<data>/secrets.json`, written atomically and `chmod 600`, owned by the service user.
- **The key is the only thing in `/etc/<app>.env`** (`<APP>_SECRET_KEY`; Paws' is `PAWS_SECRET_KEY`), so a copy of the data folder or a backup is useless without it. `python3 -m <app> make-key` prints a new one. Say in `deploy/<app>.env.example` that losing it means entering the secrets again, and changing it makes the saved ones unreadable.
- **Never sent back.** Pages only ask whether a secret is saved (`secretbox.has`), never for its value, and secrets are never logged. Once saved, the field is a password input with the placeholder "Saved – leave blank to keep" (`DESIGN-SPEC.md` 7.2), and blank keeps the saved one. A non-secret part of the same login, like the account's email, can be shown to an admin.
- **Admins only**, on the service's own Settings card, with **Save**, **Save and test**, and a **Remove** that asks first.
- **Save and test tests first, and saves only if it passes** (Paws' Tractive card: log in and list what it finds). On a failure nothing changes and the flash says why: "Not saved: …". On a pass it says what the test found.
- **Until the key is set:** saving says so and where to put it ("PAWS_SECRET_KEY isn't set in /etc/paws.env, so the login can't be saved."). Until a secret is entered, the pages that need it show a banner saying where to add it, and everything else works.
- **What isn't a secret** stays in the app's settings table. Paws' Google browser key has to reach the page to draw the map, so it isn't secret; its Google-side restrictions are what protect it, and it must differ from the server key.
