"""Design Uniformity: the Settings card's API for Flask apps.

    from du_flask import init_design
    init_design(app, "/var/lib/myapp/design")        # the state_dir in /etc/design-uniformity/<app>.toml

It adds, under the app's own login (blueprints inherit app.before_request):
    GET  /api/design/status     the card's contents
    POST /api/design/check      Check now (installs a new release even with automatic updates off)
    POST /api/design/rollback   go back to the previous version
    POST /api/design/auto       {"enabled": true|false}

The app never updates itself. These routes only read status.json and drop a flag file, which the host's
du-sync-<app>.path unit picks up and runs du_sync.py. POSTs must be JSON, so a cross-site form can't
send them. Show the card with templates/partials/design_card.html (Jinja) or your own markup, plus
static/design/design-card.js.

This file is shipped by Design Uniformity and copied in by the updater: don't edit it in the app.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request

TONES = {"ok": "success", "updated": "success", "available": "info", "blocked": "warning",
         "failed": "danger", "rolled_back": "warning"}
LABELS = {"ok": "Up to date", "updated": "Up to date", "available": "Update available",
          "blocked": "Waiting", "failed": "Check failed", "rolled_back": "Rolled back"}
STALE_AFTER = 3 * 86400        # no check for 3 days: the timer probably isn't running


def _date(iso_day: str | None) -> str:
    """2026-11-04 -> 4 Nov 2026 (spec 15)."""
    if not iso_day:
        return ""
    try:
        d = datetime.strptime(iso_day[:10], "%Y-%m-%d")
    except ValueError:
        return ""
    return f"{d.day} {d:%b %Y}"


def _ago(iso: str | None) -> str:
    """Relative time for recent things, then a date (spec 15)."""
    if not iso:
        return "never"
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return "–"
    s = (datetime.now(timezone.utc) - t).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        local = t.astimezone()
        return f"{int(s // 3600)}h ago ({local:%H:%M})"
    if s < 7 * 86400:
        return f"{int(s // 86400)}d ago"
    return _date(t.astimezone().strftime("%Y-%m-%d"))


def read_status(state_dir: str | os.PathLike) -> dict:
    """What the Settings card shows. Safe to call from a page render."""
    d = Path(state_dir)
    try:
        st = json.loads((d / "status.json").read_text())
    except (OSError, ValueError):
        st = {}
    try:
        prefs = json.loads((d / "prefs.json").read_text())
    except (OSError, ValueError):
        prefs = {}
    result = st.get("result")
    pending = (d / "check-now").exists() or (d / "rollback").exists()
    out = {
        "installed": st.get("installed"),
        "version": st.get("installed") or "Not installed yet",
        "date": _date(st.get("installed_date")),
        "available": st.get("available"),
        "previous": st.get("previous"),
        "checked": _ago(st.get("checked_at")),
        "auto": prefs.get("auto", st.get("auto", True)),
        "busy": bool(st.get("busy")) or pending,
        "message": st.get("message") or "",
        "warning": st.get("warning") or "",
        "label": LABELS.get(result, "Not checked yet"),
        "tone": TONES.get(result, "muted"),
        "can_rollback": bool(st.get("previous")),
    }
    if not st:
        out["message"] = ("The updater hasn't run on this host yet. Install it with "
                          "du_sync.py install, then use Check now.")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(st["checked_at"])).total_seconds()
        if age > STALE_AFTER and not out["busy"]:
            out.update(tone="warning", label="Not checking",
                       warning="No check for over 3 days: is the du-sync timer running on this host?")
    except (KeyError, ValueError):
        pass
    if out["busy"]:
        out.update(label="Working…", tone="info")
    return out


def git_sha(start: str | os.PathLike) -> str:
    """Short SHA of the checkout `start` is in, read from .git directly (no git binary, and no
    "dubious ownership" refusal when the app runs as a different user from the repo's owner)."""
    p = Path(start).resolve()
    for d in (p, *p.parents):
        g = d / ".git"
        if g.is_dir():
            break
    else:
        return ""
    try:
        head = (g / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head[:7]
        ref = head[5:]
        if (g / ref).is_file():
            return (g / ref).read_text().strip()[:7]
        for line in (g / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line[:7]
    except OSError:
        pass
    return ""


def runtime_facts() -> list[tuple[str, str]]:
    """The rows every About page shows under the app's name (spec 7.15)."""
    import platform
    import sqlite3
    return [("Python", platform.python_version()), ("SQLite", sqlite3.sqlite_version)]


def init_design(app, state_dir: str | os.PathLike, url_prefix: str = "/api/design", version: str | None = None):
    """Also sets the Jinja globals the sidebar footer and About page use (spec 7.15):
    app_version ("1.0.0"), app_sha() (the checkout's short SHA, read each time so an update
    shows without a restart) and runtime_facts()."""
    d = Path(state_dir)
    app.jinja_env.globals.setdefault("app_version", version or "")
    app.jinja_env.globals["app_sha"] = lambda: git_sha(app.root_path)
    app.jinja_env.globals["runtime_facts"] = runtime_facts
    bp = Blueprint("design_uniformity", __name__, url_prefix=url_prefix)

    def flag(name: str):
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def need_json():
        if not request.is_json:
            return jsonify(error="Send this as JSON."), 415
        return None

    @bp.get("/status")
    def status():
        return jsonify(read_status(d))

    @bp.post("/check")
    def check():
        if (bad := need_json()):
            return bad
        flag("check-now")
        return jsonify(read_status(d))

    @bp.post("/rollback")
    def rollback():
        if (bad := need_json()):
            return bad
        if not read_status(d)["can_rollback"]:
            return jsonify(error="There's no earlier version to go back to yet."), 409
        flag("rollback")
        return jsonify(read_status(d))

    @bp.post("/auto")
    def auto():
        if (bad := need_json()):
            return bad
        enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
        p = d / "prefs.json"
        try:
            prefs = json.loads(p.read_text())
        except (OSError, ValueError):
            prefs = {}
        prefs["auto"] = enabled
        d.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(prefs))
        os.replace(tmp, p)
        return jsonify(read_status(d))

    app.register_blueprint(bp)
    app.jinja_env.globals["design_status"] = lambda: read_status(d)
    return bp
