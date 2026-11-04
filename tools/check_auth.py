"""Checks the password and queue order on a throwaway copy of the app. Touches nothing real."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="encore-check-"))
os.environ["ENCORE_DATA"] = str(ROOT)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encore import engine  # noqa: E402
from encore import web as server  # noqa: E402

results = []
HEAD = {"X-Encore": "1"}


def expect(name, ok, detail=""):
    print("PASS" if ok else "FAIL", "|", name, "|", detail)
    results.append(bool(ok))


def post(client, path, body=None, headers=HEAD):
    response = client.post(path, json=body or {}, headers=headers)
    return response.status_code, response.get_json(silent=True)


def main():
    engine.JOBS_FILE = ROOT / "jobs.json"
    engine.MUSIC, engine.IMPORT, engine.DOWNLOADS = ROOT / "Music", ROOT / "Import", ROOT / "Encore"
    for folder in (engine.MUSIC, engine.IMPORT, engine.DOWNLOADS):
        folder.mkdir()
    app = server.create_app()                       # no start_workers: nothing runs in the background
    client = app.test_client()
    auth = app.extensions["encore"].auth

    expect("open by default", client.get("/api/queue").status_code == 200)
    expect("login page skipped when off", client.get("/login").status_code == 302)
    status, _ = post(client, "/api/password", {"new": "short"})
    expect("short password refused", status == 400)
    status, _ = post(client, "/api/password", {"new": "correct horse"})
    expect("password turned on", status == 200 and auth.enabled)
    stored = (ROOT / "auth.json").read_text()
    expect("password not stored plain", "correct horse" not in stored)
    expect("auth file private", oct((ROOT / "auth.json").stat().st_mode)[-3:] == "600")
    expect("stays signed in after turning on", client.get("/api/queue").status_code == 200)

    client.delete_cookie(server.COOKIE)
    for path in ("/api/queue", "/api/settings", "/api/status", "/api/library", "/api/plex/thumb/1"):
        status = client.get(path).status_code
        expect(f"blocked {path}", status == 401, status)
    response = client.get("/")
    expect("page goes to login", response.status_code == 302 and response.headers["Location"] == "/login")
    status, _ = post(client, "/api/settings/test")
    expect("posts blocked", status == 401)
    status, _ = post(client, "/api/password", {"new": "taken over 123"})
    expect("password can't be changed signed out", status == 401)
    response = client.get("/login")
    expect("login page shows", response.status_code == 200 and "Sign in" in response.get_data(as_text=True))
    expect("health needs no sign-in", client.get("/healthz").get_json() == {"ok": True, "busy": []})
    status, _ = post(client, "/api/login", {"password": "x"}, headers={})
    expect("posts without the page's header refused", status == 403)

    client.set_cookie(server.COOKIE, "9999999999.deadbeef")
    expect("forged session refused", client.get("/api/queue").status_code == 401)
    client.set_cookie(server.COOKIE, "1." + auth.sign(1))
    expect("expired session refused", client.get("/api/queue").status_code == 401)
    client.delete_cookie(server.COOKIE)

    status, _ = post(client, "/api/login", {"password": "wrong"})
    expect("wrong password refused", status == 401)
    response = client.post("/api/login", json={"password": "correct horse"}, headers=HEAD)
    expect("right password works", response.status_code == 200)
    expect("session works", client.get("/api/queue").status_code == 200)
    set_cookie = response.headers["Set-Cookie"]
    expect("cookie hidden from scripts", "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie, set_cookie)

    status, _ = post(client, "/api/password", {"current": "nope", "new": "another pass"})
    expect("change needs current password", status == 403)
    old_cookie = client.get_cookie(server.COOKIE).value
    status, _ = post(client, "/api/password", {"current": "correct horse", "new": "another pass"})
    expect("change works", status == 200 and auth.check("another pass") and not auth.check("correct horse"))
    expect("old sessions end", not auth.valid(old_cookie))

    post(client, "/api/logout")
    for _ in range(5):
        post(client, "/api/login", {"password": "guess"})
    status, body = post(client, "/api/login", {"password": "another pass"})
    expect("locked after five wrong tries", status == 429, body)
    auth.fails.clear()
    status, _ = post(client, "/api/login", {"password": "another pass"})
    expect("back in after the wait", status == 200)

    queue = app.extensions["encore"].queue
    make = lambda name, state: {"id": name, "state": state, "title": name, "added": 1}
    queue.jobs = [make("done1", "done"), make("now", "downloading"), make("a", "queued"), make("b", "queued"), make("c", "queued")]
    post(client, "/api/queue/order", {"ids": ["c", "a", "b"]})
    expect("queue reordered", [j["id"] for j in queue.jobs] == ["done1", "now", "c", "a", "b"], [j["id"] for j in queue.jobs])
    post(client, "/api/queue/order", {"ids": ["b", "now", "c"]})
    order = [j["id"] for j in queue.jobs]
    expect("running job can't move", order == ["done1", "now", "b", "c", "a"], order)
    next_up = next(j for j in queue.jobs if j["state"] == "queued")
    expect("worker takes the new first", next_up["id"] == "b")

    server.TRUSTED_PROXY = "127.0.0.1"
    outside = {**HEAD, "CF-Connecting-IP": "203.0.113.9", "X-Forwarded-Proto": "https"}
    for _ in range(5):
        post(client, "/api/login", {"password": "guess"}, outside)
    status, _ = post(client, "/api/login", {"password": "another pass"}, outside)
    expect("attacker behind the tunnel is locked", status == 429)
    status, _ = post(client, "/api/login", {"password": "another pass"}, {**outside, "CF-Connecting-IP": "203.0.113.10"})
    expect("someone else can still sign in", status == 200)
    response = client.post("/api/login", json={"password": "another pass"}, headers={**outside, "CF-Connecting-IP": "203.0.113.11"})
    expect("cookie is HTTPS-only through the tunnel", "Secure" in response.headers["Set-Cookie"])
    expect("page can't be framed", response.headers["X-Frame-Options"] == "DENY")
    server.TRUSTED_PROXY = ""
    auth.fails.clear()
    for _ in range(5):
        post(client, "/api/login", {"password": "guess"}, {**HEAD, "CF-Connecting-IP": "203.0.113.12"})
    status, _ = post(client, "/api/login", {"password": "another pass"}, {**HEAD, "CF-Connecting-IP": "203.0.113.13"})
    expect("header ignored without a trusted proxy", status == 429)
    auth.fails.clear()
    post(client, "/api/login", {"password": "another pass"})

    status, _ = post(client, "/api/password", {"current": "wrong", "new": ""})
    expect("removing needs current password", status == 403 and auth.enabled)
    status, _ = post(client, "/api/password", {"current": "another pass", "new": ""})
    expect("password removed", status == 200 and not auth.enabled and not (ROOT / "auth.json").exists())
    client.delete_cookie(server.COOKIE)
    expect("open again", client.get("/api/queue").status_code == 200)


main()
sys.exit(0 if all(results) else 1)
