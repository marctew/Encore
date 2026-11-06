"""Checks Encore's outside API (docs/API.md) on a throwaway copy of the app, against fakes.

Tokens made through the Settings forms, the token rules, every feed, image, action and event, the
queue's events and arrivals, webhook delivery and signing, and the downloads display page. Touches
nothing real and sends nothing anywhere: covers, indexers and webhooks are local fakes.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(tempfile.mkdtemp(prefix="encore-check-api-"))
os.environ["ENCORE_DATA"] = str(ROOT / "data")
os.environ["ENCORE_CACHE"] = str(ROOT / "cache")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, request as hook_request  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from encore import api, engine  # noqa: E402
from encore import web as server  # noqa: E402
from encore.du_api import verify  # noqa: E402

results = []
HEAD = {"X-Encore": "1"}
ALBUM = "33333333-3333-3333-3333-333333333333"
OTHER = "44444444-4444-4444-4444-444444444444"
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def expect(name, ok, detail=""):
    print("PASS" if ok else "FAIL", "|", name, "|", detail)
    results.append(bool(ok))


def details(album_id, title):
    track = {"disc": 1, "number": 1, "title": "One", "seconds": 200, "artist": "Test Band"}
    return {"id": album_id, "title": title, "artist": "Test Band", "artist_id": "", "year": "1999", "kind": "Album",
            "cover": "", "tracks": 1, "tracks_list": [track], "seconds": 200, "minutes": 3, "label": "", "released": "1999",
            "discs": 1, "editions": [{"id": "e", "title": title, "date": "1999", "status": "Official", "label": "", "tracks": [track]}]}


class FakeWeb:
    """The Cover Art Archive, with a real JPEG."""

    def __init__(self):
        path = ROOT / "cover.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=c=blue:size=600x600", "-frames:v", "1",
                        str(path)], check=True)
        self.jpeg, self.asked = path.read_bytes(), []

    def get(self, url, **_):
        self.asked.append(url)
        return SimpleNamespace(status_code=200, content=self.jpeg)


def token_from(response):
    found = re.search(r'value="(enc_[^"]+)"', response.get_data(as_text=True))
    return found.group(1) if found else None


def main():
    (ROOT / "data").mkdir(parents=True)
    for name in ("Music", "Import", "Encore"):
        (ROOT / name).mkdir()
    engine.JOBS_FILE = ROOT / "data" / "jobs.json"
    app = server.create_app()
    s = app.extensions["encore"]
    client = app.test_client()
    client.post("/api/settings", headers=HEAD, json={
        "folders": {"music": str(ROOT / "Music"), "imports": str(ROOT / "Import"), "downloads": str(ROOT / "Encore")},
        "sab": {"url": "http://sab.invalid", "key": "k"}, "indexers": [{"name": "Fake", "url": "http://idx.invalid", "key": "k"}]})
    albums = {ALBUM: details(ALBUM, "First Album"), OTHER: details(OTHER, "Second Album")}
    s.brainz.album = lambda album_id, *_, **__: albums[album_id]
    s.brainz.web = FakeWeb()
    s.indexers.search = lambda artist, title: ([{"title": f"{artist} - {title} (1999) FLAC", "size": 3 * 10**8, "key": f"k-{title}",
                                                 "indexer": "Fake", "guid": "g", "posted": 0, "grabs": 5}]
                                               if title == "First Album" else [], [])

    # Tokens, made the way the Settings card makes them
    page = client.get("/settings").get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    expect("Settings shows the API tokens and Webhooks cards", "API tokens" in page and "Webhooks" in page)
    expect("the form's CSRF token isn't the old fixed one", csrf != "1" and len(csrf) >= 16)
    refused = client.post("/settings/api/tokens", data={"name": "Kitchen", "scope": "read"})
    expect("a token form without the CSRF token is refused", refused.status_code == 403)
    made = client.post("/settings/api/tokens", data={"name": "Kitchen", "scope": "read", "csrf_token": csrf}, follow_redirects=True)
    read = token_from(made)
    made = client.post("/settings/api/tokens", data={"name": "Home Assistant", "scope": "write", "csrf_token": csrf}, follow_redirects=True)
    write = token_from(made)
    expect("a new token is shown once", read and write and read.startswith("enc_"))
    expect("and not again", token_from(client.get("/settings")) is None)
    expect("only its hash is stored", read not in (ROOT / "data" / "encore.db").read_bytes().decode("latin-1"))
    R, W = {"Authorization": f"Bearer {read}"}, {"Authorization": f"Bearer {write}"}

    # The gate
    expect("no token, no answer", client.get("/api/v1/status").status_code == 401)
    bad = client.get("/api/v1/status", headers={"Authorization": "Bearer enc_nope"})
    expect("a bad token gets the standard's sentence", bad.status_code == 401 and "Settings → API tokens" in bad.get_json()["error"])
    expect("a read token works in the URL", client.get(f"/api/v1/status?token={read}").status_code == 200)
    expect("a write token is refused in the URL", client.get(f"/api/v1/status?token={write}").status_code == 400)
    expect("a read token can't act", client.post("/api/v1/actions/want_album", headers=R, json={"album_id": ALBUM}).status_code == 403)
    first = client.get("/api/v1/status", headers=R)
    again = client.get("/api/v1/status", headers={**R, "If-None-Match": first.headers["ETag"]})
    expect("ETag and 304", first.headers.get("ETag") and again.status_code == 304, again.status_code)
    expect("the page's own API is untouched", client.get("/api/queue").status_code == 200)

    # meta and status
    meta = client.get("/api/v1/meta", headers=R).get_json()
    expect("meta lists the feeds, actions, display and events",
           [f["name"] for f in meta["feeds"]] == ["downloads", "arrivals", "failed", "wanted"]
           and [a["name"] for a in meta["actions"]] == ["want_album", "get_album", "retry_job", "search_wanted"]
           and meta["displays"][0]["name"] == "downloads" and "download.finished" in meta["events"]
           and meta["accent"] == "blue" and meta["api"] == 1, meta)
    status = client.get("/api/v1/status", headers=R).get_json()
    expect("status says nothing's happening", status["state"] == "ok" and status["headline"] == "Nothing downloading"
           and ISO.match(status["generated_at"]) and len(status["counts"]) <= 6, status)

    # Actions
    events_before = s.store.one("SELECT COALESCE(MAX(id), 0) AS id FROM events")["id"]
    wanted = client.post("/api/v1/actions/want_album", headers=W, json={"album_id": OTHER}).get_json()
    expect("want_album adds it", wanted["ok"] and OTHER in s.store.wanted_ids(), wanted)
    expect("a bad album_id is refused with a sentence",
           client.post("/api/v1/actions/want_album", headers=W, json={"album_id": "nope"}).get_json()["code"] == "bad_request")
    key = {**W, "Idempotency-Key": "panel-press-1"}
    got = client.post("/api/v1/actions/get_album", headers=key, json={"album_id": ALBUM}).get_json()
    repeat = client.post("/api/v1/actions/get_album", headers=key, json={"album_id": ALBUM}).get_json()
    jobs = [j for j in s.queue.jobs if j["album_id"] == ALBUM]
    expect("get_album queues the good release", got["result"]["queued"] and len(jobs) == 1, got)
    expect("the same Idempotency-Key does it once", repeat == got and len([j for j in s.queue.jobs if j["album_id"] == ALBUM]) == 1)
    nothing = client.post("/api/v1/actions/get_album", headers=W, json={"album_id": OTHER}).get_json()
    expect("get_album with nothing good leaves it on Wanted", not nothing["result"]["queued"] and "Wanted" in nothing["message"], nothing)
    expect("unknown action", client.post("/api/v1/actions/delete_everything", headers=W, json={}).status_code == 404)
    log = s.store.rows("SELECT * FROM api_actions")
    expect("actions are logged with the token's name", len(log) >= 4 and log[0]["token_name"] == "Home Assistant", [dict(r) for r in log][:2])

    # Feeds
    downloads = client.get("/api/v1/feeds/downloads", headers=R).get_json()
    item = downloads["items"][0]
    expect("downloads lists the queued album", item["title"] == "Test Band – First Album" and item["meta"]["state"] == "queued"
           and item["image"] == f"/api/v1/images/cover/{ALBUM}" and ISO.match(item["at"]) and item["at_label"].startswith("Today"), item)
    wanted_feed = client.get("/api/v1/feeds/wanted", headers=R).get_json()
    expect("wanted lists it", [i["meta"]["album_id"] for i in wanted_feed["items"]] == [OTHER], wanted_feed)
    expect("an unknown feed", client.get("/api/v1/feeds/arivals", headers=R).get_json()["code"] == "not_found")
    expect("limit is checked", client.get("/api/v1/feeds/downloads?limit=x", headers=R).status_code == 400)

    # The queue's own changes become events and arrivals
    job = jobs[0]
    s.queue.update(job, state="downloading")
    s.queue.update(job, state="done", where="library", folder="Test Band/First Album (1999)", quality="16/44.1", finished=time.time(),
                   message="In your library.")
    for n in range(3):
        fake = {**job, "id": f"old{n}", "title": f"Old {n}", "state": "queued"}
        s.queue.update(fake, state="done", where="library", finished=time.time() - 100 * (n + 1), message="In your library.")
    types = [r["type"] for r in s.store.rows("SELECT type FROM events WHERE id > ? ORDER BY id", events_before)]
    expect("the queue publishes its changes", types[:5] == ["wanted.added", "job.queued", "wanted.added", "download.started",
                                                             "download.finished"], types)
    arrivals = client.get("/api/v1/feeds/arrivals?limit=2", headers=R).get_json()
    expect("arrivals, newest first, a page at a time", [i["meta"]["album"] for i in arrivals["items"]] == ["First Album", "Old 0"]
           and arrivals["next_cursor"], arrivals)
    rest = client.get(f"/api/v1/feeds/arrivals?limit=5&cursor={arrivals['next_cursor']}", headers=R).get_json()
    expect("the cursor gives the rest", [i["meta"]["album"] for i in rest["items"]] == ["Old 1", "Old 2"] and rest["next_cursor"] is None, rest)
    since = client.get(f"/api/v1/feeds/arrivals?since={arrivals['items'][1]['at']}", headers=R).get_json()
    expect("since gives what's newer", [i["meta"]["album"] for i in since["items"]] == ["First Album"], since)
    s.queue.jobs.clear()
    expect("arrivals outlive Clear finished", len(client.get("/api/v1/feeds/arrivals", headers=R).get_json()["items"]) == 4)

    # Images
    webp = client.get(f"/api/v1/images/cover/{ALBUM}?w=300", headers={**R, "Accept": "image/webp"})
    jpeg = client.get(f"/api/v1/images/cover/{ALBUM}?w=300&fmt=jpeg&token={read}")
    expect("covers as WebP, snapped to 320", webp.status_code == 200 and webp.mimetype == "image/webp", webp.status_code)
    expect("and JPEG for small screens, with a URL token", jpeg.status_code == 200 and jpeg.data[:2] == b"\xff\xd8")
    asked = len(s.brainz.web.asked)
    client.get(f"/api/v1/images/cover/{ALBUM}?w=300", headers={**R, "Accept": "image/webp"})
    expect("cached on disk", len(s.brainz.web.asked) == asked and any((ROOT / "cache" / "images").iterdir()))
    expect("an id that isn't one", client.get("/api/v1/images/cover/..%2Fetc", headers=R).status_code == 404)

    # The events stream
    stream = client.get("/api/v1/events?last_event_id=0", headers=R, buffered=False)
    text = ""
    for chunk in stream.response:
        text += chunk.decode() if isinstance(chunk, bytes) else chunk
        if "download.finished" in text:
            break
    stream.close()
    expect("events replay after Last-Event-ID", "event: job.queued" in text and "event: download.finished" in text, text[:200])

    # Webhooks, delivered and signed
    got_hooks = []
    hooks = Flask("hooks")

    @hooks.post("/hook")
    def hook():
        got_hooks.append((hook_request.get_data(), hook_request.headers.get("X-Signature")))
        return "", 200 if len(got_hooks) > 1 else 500

    runner = make_server("127.0.0.1", 18998, hooks, threaded=True)
    threading.Thread(target=runner.serve_forever, daemon=True).start()
    made = client.post("/settings/webhooks", data={"name": "HA", "url": "http://127.0.0.1:18998/hook", "csrf_token": csrf,
                                                   "event_types": ["download.finished"]}, follow_redirects=True)
    secret = re.search(r'readonly value="([^"]+)"', made.get_data(as_text=True)).group(1)
    with app.test_request_context():
        api.publish("download.started", "Not wanted by this hook", {"id": "x", "title": "x"})
        api.publish("download.finished", "Test Band – First Album is in your library", {"id": "job:1", "title": "Test Band – First Album"})
        store = s.store
        api.deliver_once(store)
        store.db.execute("UPDATE webhook_deliveries SET next_at = 0 WHERE done = 0")
        api.deliver_once(store)
    body, signature = got_hooks[-1]
    expect("webhooks get only their event types, and retry", len(got_hooks) == 2 and json.loads(body)["type"] == "download.finished",
           [json.loads(b)["type"] for b, _ in got_hooks])
    expect("signed with the secret", verify(body, secret, signature))
    card = client.get("/settings").get_data(as_text=True)
    expect("the card shows it was sent", "sent just now" in card and "http://127.0.0.1:18998/…hook" in card)

    # The display page
    shown = client.get(f"/display/downloads?token={read}&theme=light")
    page = shown.get_data(as_text=True)
    expect("the display page shows", shown.status_code == 200 and "Nothing downloading" in page and 'data-theme="light"' in page
           and "First Album" in page and f"token={read}" in page, shown.status_code)
    expect("with no referrer and no caching", shown.headers.get("Referrer-Policy") == "no-referrer" and shown.headers.get("Cache-Control") == "no-store")
    fragment = client.get(f"/display/downloads?token={read}&fragment=1").get_data(as_text=True)
    expect("its body reloads alone", "<html" not in fragment and "dl-display" in fragment)
    expect("without a token it says what to do, as a page", "needs a read token" in client.get("/display/downloads").get_data(as_text=True))

    # The page's session still works for the API, and a password keeps strangers out
    client.post("/api/password", headers=HEAD, json={"new": "correct horse battery"})
    signed_in = client.get("/api/v1/status")
    client.delete_cookie(server.COOKIE)
    expect("an admin session reads the API", signed_in.status_code == 200)
    expect("with a password on, tokens still work", client.get("/api/v1/status", headers=R).status_code == 200)
    expect("and nothing else gets in", client.get("/api/v1/status").status_code == 401 and client.get("/api/queue").status_code == 401)
    revoke = client.post(f"/settings/api/tokens/1/revoke", data={"csrf_token": csrf})
    expect("revoking needs a signed-in session", revoke.status_code in (401, 302))

    print(f"\n{sum(results)} of {len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
