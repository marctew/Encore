# App standard

How Marc's homelab apps are built, so every app is put together, run and updated the same way. `DESIGN-SPEC.md` covers how they look; this covers everything underneath.

**The reference app is The Back Room** (`marctew/backroom`, LXC 455). When this file and an app disagree, this file wins; when this file is silent, copy The Back Room.

## 1. The stack

| | Standard | Why |
|---|---|---|
| Language | Python 3 from the distro, with packages from `apt` where it has them | One toolchain to keep up to date. When an app needs packages `apt` lacks (or too old), it uses `.venv/` in the checkout, filled from `requirements.txt` by `scripts/update.sh`, as Vital Signs does. |
| Web | Flask, Jinja templates rendered on the server | Every app is already Flask, or is being rewritten into it. |
| Server | gunicorn, **one worker**, many threads (`--threads 32`) | Apps keep jobs and caches in memory, so there must be one process. Threads let video streams and slow requests run side by side. |
| Process | a systemd service, one app per LXC | Restarts, logs and start-up order come free. |
| CSS | `static/design/uniform.css` plus the app's own `static/<app>.css` | No build step, so a design update is just files. |
| JS | Plain JavaScript files in `static/`; no framework, no bundler | Same reason. |
| Data | SQLite or JSON files in `/var/lib/<app>/` | No database server to run. |

**The exception is Binge Stats**, which other people install from GHCR. It stays in Docker and on Tailwind (`css/tailwind-components.css` maps Tailwind onto the same design), but the rest of this file still applies: `/healthz`, `scripts/update.sh`, the Settings card and the repo layout.

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
  docs/design/           DESIGN-SPEC.md and this file, copied in by the design updater
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
| Secrets | `/etc/<app>.env`, `chmod 600 root`, or a config file in `/etc/<app>/` when settings are richer than a list of variables (Vital Signs' `server.toml`). Never in the repo, never in the unit, never in a page. |
| Data | `/var/lib/<app>/` |
| Caches | `/var/cache/<app>/` |
| Design updater state | `/var/lib/<app>/design/` |
| Logs | the journal: `journalctl -u <service>` |

New apps use one name for the folder, service and env file. Older apps keep the names they have (The Back Room is `hmm-browser`) rather than risk a rename.

**Apps that serve large files with Range requests** (video, audio): gunicorn's `wsgi.file_wrapper` can't seek, so Werkzeug's `send_file` reads and discards everything before the requested range. That made a seek halfway into a 300 MB video on NFS take 20 seconds in The Back Room. Hand `send_file` Werkzeug's own wrapper instead; copy `_seekable_files` from The Back Room's `backroom/web.py`, and time a mid-file Range request before switching an app to gunicorn.

**Background jobs** (rescans, imports, polling) start in `start_workers()`, which `wsgi.py` calls once in the gunicorn worker. Each job keeps its state in a dict with a `"running"` flag, and `health.py` lists those flags.

## 4. What every app has

- **Login:** a session cookie named after the app, `SameSite=Lax` and 90 days long, signed with a key kept in `/var/lib/<app>/`. The password is stored only as a hash and changed in Settings. Changing it logs out every other session. Every request is checked in one `before_request`, which lets through only `login`, `static` and `healthz`. Pages redirect to `/login?next=…`; `/api/*` answers `401 {"error": "Log in again."}`.
- **`/healthz`:** no login, and it says nothing about the app's contents: `{"ok": true, "busy": ["import"]}`. `busy` names any job a restart now would cut short.
- **Settings page:** the app's own settings, then the password, then the **Design system** card (`DESIGN-SPEC.md` 7.14) last.
- **A version and an About page** (`DESIGN-SPEC.md` 7.15): `__version__` in the package, passed to `init_design(..., version=__version__)`, the shared sidebar footer (Sign out, theme switch, `v1.0.0 · sha · About`) and `/about`.
- **JSON APIs:** actions are `POST` with a JSON body, answering JSON. Errors are `{"error": "…"}` with a 4xx status, written for a person to read.
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

> Built to `docs/design/APP-STANDARD.md` and styled to `docs/design/DESIGN-SPEC.md` (from `C:/ClaudeCowork/Design Uniformity`). Follow both; no one-off styles or layouts.

| App | Status |
|---|---|
| The Back Room | Reference app, 2026-11-04 |
| Vital Signs | Done 2026-11-04: gunicorn, `/healthz`, `scripts/update.sh`, restyled. Keeps its venv, TOML config and `server/` + `agent/` layout |
| PodBridge | Moves from Docker to systemd |
| Encore | Rewrite from aiohttp |
| Binge Stats | Commit today's changes first; stays in Docker (section 1) |
| claude-ops | Rewrite from uvicorn, last; design updates by hand only |
