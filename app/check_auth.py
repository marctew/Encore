"""Checks the password and queue order on a throwaway copy of the app. Touches nothing real."""
import asyncio
import sys
import tempfile
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

import server

results = []
HEAD = {"X-Encore": "1"}


def expect(name, ok, detail=""):
    print("PASS" if ok else "FAIL", "|", name, "|", detail)
    results.append(bool(ok))


async def post(client, path, body=None, headers=HEAD):
    response = await client.post(path, json=body or {}, headers=headers)
    return response.status, await response.json()


async def main():
    root = Path(tempfile.mkdtemp(prefix="encore-check-"))
    server.DATA, server.JOBS_FILE, server.PHOTOS = root, root / "jobs.json", root / "photos"
    server.MUSIC, server.IMPORT, server.DOWNLOADS = root / "Music", root / "Import", root / "Encore"
    for folder in (server.MUSIC, server.IMPORT, server.DOWNLOADS, server.PHOTOS):
        folder.mkdir()
    app = server.make_app()
    client = TestClient(TestServer(app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    app["worker"].cancel()
    auth = app[server.auth_key]

    response = await client.get("/api/queue")
    expect("open by default", response.status == 200)
    response = await client.get("/login", allow_redirects=False)
    expect("login page skipped when off", response.status == 302)
    status, _ = await post(client, "/api/password", {"new": "short"})
    expect("short password refused", status == 400)
    status, _ = await post(client, "/api/password", {"new": "correct horse"})
    expect("password turned on", status == 200 and auth.enabled)
    stored = (root / "auth.json").read_text()
    expect("password not stored plain", "correct horse" not in stored)
    expect("auth file private", oct((root / "auth.json").stat().st_mode)[-3:] == "600")
    response = await client.get("/api/queue")
    expect("stays signed in after turning on", response.status == 200)

    client.session.cookie_jar.clear()
    for path in ("/api/queue", "/api/settings", "/api/status", "/api/library", "/api/plex/thumb/1"):
        response = await client.get(path)
        expect(f"blocked {path}", response.status == 401, response.status)
    response = await client.get("/", allow_redirects=False)
    expect("page goes to login", response.status == 302 and response.headers["Location"] == "/login")
    status, _ = await post(client, "/api/settings/test")
    expect("posts blocked", status == 401)
    status, _ = await post(client, "/api/password", {"new": "taken over 123"})
    expect("password can't be changed signed out", status == 401)
    response = await client.get("/login")
    expect("login page shows", response.status == 200 and "Sign in" in await response.text())

    forged = aiohttp.CookieJar(unsafe=True)
    client.session.cookie_jar.update_cookies({server.COOKIE: "9999999999.deadbeef"})
    response = await client.get("/api/queue")
    expect("forged session refused", response.status == 401)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({server.COOKIE: "1." + auth.sign(1)})
    response = await client.get("/api/queue")
    expect("expired session refused", response.status == 401)
    client.session.cookie_jar.clear()

    status, _ = await post(client, "/api/login", {"password": "wrong"})
    expect("wrong password refused", status == 401)
    status, _ = await post(client, "/api/login", {"password": "correct horse"})
    expect("right password works", status == 200)
    response = await client.get("/api/queue")
    expect("session works", response.status == 200)
    cookie = next(c for c in client.session.cookie_jar if c.key == server.COOKIE)
    expect("cookie hidden from scripts", cookie["httponly"] and cookie["samesite"] == "Lax")

    status, _ = await post(client, "/api/password", {"current": "nope", "new": "another pass"})
    expect("change needs current password", status == 403)
    old_cookie = cookie.value
    status, _ = await post(client, "/api/password", {"current": "correct horse", "new": "another pass"})
    expect("change works", status == 200 and auth.check("another pass") and not auth.check("correct horse"))
    expect("old sessions end", not auth.valid(old_cookie))

    await post(client, "/api/logout")
    for _ in range(5):
        await post(client, "/api/login", {"password": "guess"})
    status, body = await post(client, "/api/login", {"password": "another pass"})
    expect("locked after five wrong tries", status == 429, body)
    auth.fails.clear()
    status, _ = await post(client, "/api/login", {"password": "another pass"})
    expect("back in after the wait", status == 200)

    queue = app[server.queue_key]
    make = lambda name, state: {"id": name, "state": state, "title": name, "added": 1}
    queue.jobs = [make("done1", "done"), make("now", "downloading"), make("a", "queued"), make("b", "queued"), make("c", "queued")]
    status, _ = await post(client, "/api/queue/order", {"ids": ["c", "a", "b"]})
    expect("queue reordered", [j["id"] for j in queue.jobs] == ["done1", "now", "c", "a", "b"], [j["id"] for j in queue.jobs])
    await post(client, "/api/queue/order", {"ids": ["b", "now", "c"]})
    order = [j["id"] for j in queue.jobs]
    expect("running job can't move", order == ["done1", "now", "b", "c", "a"], order)
    next_up = next(j for j in queue.jobs if j["state"] == "queued")
    expect("worker takes the new first", next_up["id"] == "b")

    server.TRUSTED_PROXY = "127.0.0.1"
    outside = {**HEAD, "CF-Connecting-IP": "203.0.113.9", "X-Forwarded-Proto": "https"}
    for _ in range(5):
        await post(client, "/api/login", {"password": "guess"}, outside)
    status, _ = await post(client, "/api/login", {"password": "another pass"}, outside)
    expect("attacker behind the tunnel is locked", status == 429)
    status, _ = await post(client, "/api/login", {"password": "another pass"}, {**outside, "CF-Connecting-IP": "203.0.113.10"})
    expect("someone else can still sign in", status == 200)
    response = await client.post("/api/login", json={"password": "another pass"}, headers={**outside, "CF-Connecting-IP": "203.0.113.11"})
    expect("cookie is HTTPS-only through the tunnel", "Secure" in response.headers["Set-Cookie"])
    expect("page can't be framed", response.headers["X-Frame-Options"] == "DENY")
    server.TRUSTED_PROXY = ""
    auth.fails.clear()
    for _ in range(5):
        await post(client, "/api/login", {"password": "guess"}, {**HEAD, "CF-Connecting-IP": "203.0.113.12"})
    status, _ = await post(client, "/api/login", {"password": "another pass"}, {**HEAD, "CF-Connecting-IP": "203.0.113.13"})
    expect("header ignored without a trusted proxy", status == 429)
    auth.fails.clear()
    await post(client, "/api/login", {"password": "another pass"})

    status, _ = await post(client, "/api/password", {"current": "wrong", "new": ""})
    expect("removing needs current password", status == 403 and auth.enabled)
    status, _ = await post(client, "/api/password", {"current": "another pass", "new": ""})
    expect("password removed", status == 200 and not auth.enabled and not (root / "auth.json").exists())
    client.session.cookie_jar.clear()
    response = await client.get("/api/queue")
    expect("open again", response.status == 200)
    await client.close()


asyncio.run(main())
sys.exit(0 if all(results) else 1)
