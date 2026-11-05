"""Builds albums from YouTube links against a fake yt-dlp (tools/fake_ytdlp.py) in throwaway folders.

The fake makes real mp4 files with ffmpeg, so checking, tagging and filing all run for real.
Touches nothing real and sends nothing anywhere.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(tempfile.mkdtemp(prefix="encore-check-yt-"))
os.environ["ENCORE_DATA"] = str(ROOT / "data")
os.environ["FAKE_YT"] = str(ROOT / "fake")
os.environ["ENCORE_YTDLP"] = str(Path(__file__).resolve().parent / "fake_ytdlp.py")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mutagen.mp4 import MP4  # noqa: E402

from encore import engine  # noqa: E402
from encore import web as server  # noqa: E402
from encore.youtube import youtube_id  # noqa: E402

results = []
HEAD = {"X-Encore": "1"}
ALBUM_ID = "22222222-2222-2222-2222-222222222222"
TITLES = ["Come Together", "Something", "Oh! Darling"]
FAKE = ROOT / "fake"
VIDEOS = {
    "aaaaaaaaaa1": {"title": "Test Band - Come Together (Official Video)", "channel": "Test Band", "duration": 8},
    "aaaaaaaaaa2": {"title": "Something", "channel": "Test Band - Topic", "duration": 9},
    "aaaaaaaaaa3": {"title": "Test Band - Oh! Darling", "channel": "Test Band", "duration": 10},
    "aaaaaaaaaa4": {"title": "Test Band - Something (Live)", "channel": "Fan Uploads", "duration": 30},
    "brokenvide0": {"title": "Test Band - Something", "channel": "Test Band", "duration": 9, "fail": "HTTP Error 403: Forbidden"},
    "slowvideo00": {"title": "Test Band - Oh! Darling", "channel": "Test Band", "duration": 10, "slow": True},
}


def expect(name, ok, detail=""):
    print("PASS" if ok else "FAIL", "|", name, "|", detail)
    results.append(bool(ok))


def album():
    tracks = [{"disc": 1, "number": n + 1, "title": t, "seconds": 8 + n, "artist": "Test Band"} for n, t in enumerate(TITLES)]
    edition = {"id": "e1", "title": "Can't Stop", "date": "1969-09-26", "status": "Official", "label": "Fake Records", "tracks": tracks}
    return {"id": ALBUM_ID, "title": "Can't Stop", "artist": "Test Band", "artist_id": "", "year": "1969", "kind": "Album",
            "cover": "", "tracks": 3, "tracks_list": tracks, "seconds": 27, "editions": [edition], "minutes": 0, "label": "",
            "released": "1969", "discs": 1}


class FakeWeb:
    """The Cover Art Archive, with a real JPEG."""

    def __init__(self):
        path = ROOT / "cover.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=c=red:size=64x64", "-frames:v", "1", str(path)], check=True)
        self.jpeg = path.read_bytes()

    def get(self, url, **_):
        return SimpleNamespace(status_code=200, content=self.jpeg)


def downloads():
    log = FAKE / "downloads.log"
    return log.read_text().split() if log.exists() else []


def link(client, key, url):
    response = client.post(f"/api/album/{ALBUM_ID}/youtube/link", json={"key": key, "url": url}, headers=HEAD)
    return response.status_code, response.get_json(silent=True) or response.get_data(as_text=True)


def wait(queue, job_id, states=("done", "failed")):
    for _ in range(1200):
        time.sleep(0.05)
        job = queue.get(job_id)
        if job["state"] in states and (states != ("done", "failed") or queue.running is None):
            return job
    raise TimeoutError(job_id)


def main():
    FAKE.mkdir(parents=True)
    (FAKE / "videos.json").write_text(json.dumps(VIDEOS))
    music, imports, downloads_dir = ROOT / "Music", ROOT / "Import", ROOT / "Encore"
    for folder in (music, imports, downloads_dir):
        folder.mkdir()
    engine.JOBS_FILE = ROOT / "data" / "jobs.json"
    (ROOT / "data").mkdir(exist_ok=True)

    # The link rules, straight
    good = ["https://www.youtube.com/watch?v=aaaaaaaaaa1", "youtube.com/watch?v=aaaaaaaaaa1&t=30s", "https://youtu.be/aaaaaaaaaa1?si=x",
            "https://m.youtube.com/watch?v=aaaaaaaaaa1", "https://music.youtube.com/watch?v=aaaaaaaaaa1&list=RD",
            "https://www.youtube.com/shorts/aaaaaaaaaa1", "https://www.youtube.com/live/aaaaaaaaaa1", "aaaaaaaaaa1"]
    expect("every kind of YouTube link is read", all(youtube_id(u) == "aaaaaaaaaa1" for u in good), [youtube_id(u) for u in good])
    bad = ["https://evil.example/watch?v=aaaaaaaaaa1", "--exec rm -rf ~", "https://www.youtube.com/playlist?list=PL123",
           "https://youtube.com.evil.example/watch?v=aaaaaaaaaa1", "https://www.youtube.com/watch?v=short", "file:///etc/passwd",
           "https://www.youtube.com/watch?v=aaaaaaaaaa1;ls", "https://www.youtube.com/@channel"]
    expect("anything else is refused", not any(youtube_id(u) for u in bad), [youtube_id(u) for u in bad])

    app = server.create_app()
    s = app.extensions["encore"]
    client = app.test_client()
    client.post("/api/settings", json={"folders": {"music": str(music), "imports": str(imports), "downloads": str(downloads_dir)}}, headers=HEAD)
    s.brainz.album = lambda *_, **__: album()
    s.brainz.web = FakeWeb()
    threading.Thread(target=s.queue.run, daemon=True).start()

    built = client.get(f"/api/album/{ALBUM_ID}/youtube").get_json()
    expect("builder lists the tracks", [t["title"] for t in built["tracks"]] == TITLES and built["edition"] == "e1", built)
    expect("edition is described", built["editions"][0]["label"] == "3 tracks · 1969 · Fake Records", built["editions"])

    status, body = link(client, "1-1", "https://evil.example/watch?v=aaaaaaaaaa1")
    expect("a link to another site is refused", status == 400, body)
    status, body = link(client, "1-1", "https://www.youtube.com/watch?v=nosuchvideo")
    expect("a missing video says so", status == 502 and "removed or made private" in body["error"], body)
    status, body = link(client, "9-9", "aaaaaaaaaa1")
    expect("a track that isn't there is refused", status == 404, body)
    status, body = link(client, "1-1", "https://youtu.be/aaaaaaaaaa1")
    expect("a good link is looked up", status == 200 and body["video"]["title"].startswith("Test Band - Come") and body["video"]["fit"] == "good", body)

    found = client.post(f"/api/album/{ALBUM_ID}/youtube/find", json={"key": "1-2"}, headers=HEAD).get_json()["results"]
    ids = [v["id"] for v in found]
    expect("Find puts the Topic upload first", ids[0] == "aaaaaaaaaa2" and found[0]["audio_only"], ids)
    expect("Find puts the live fan upload last", ids[-1] == "aaaaaaaaaa4" and found[-1]["fit"] == "off", [(v["id"], v["score"]) for v in found])

    response = client.post("/api/queue/youtube", json={"album_id": ALBUM_ID}, headers=HEAD)
    expect("queueing with blank tracks needs a yes", response.status_code == 409, response.get_data(as_text=True))
    link(client, "1-2", "aaaaaaaaaa2")
    link(client, "1-3", "aaaaaaaaaa3")
    saved = client.get(f"/api/album/{ALBUM_ID}/youtube").get_json()
    expect("the draft is kept", all(t["video"] for t in saved["tracks"]))

    # The whole way: download, check, tag, file, videos
    job = client.post("/api/queue/youtube", json={"album_id": ALBUM_ID}, headers=HEAD).get_json()
    again = client.post("/api/queue/youtube", json={"album_id": ALBUM_ID}, headers=HEAD)
    expect("can't queue it twice", again.status_code == 409)
    job = wait(s.queue, job["id"])
    expect("album built and filed", job["state"] == "done" and job["where"] == "library", job["message"])
    folder = music / job["folder"]
    names = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
    expect("tracks named from MusicBrainz", names == ["01 - Come Together.m4a", "02 - Something.m4a", "03 - Oh! Darling.m4a", "cover.jpg"], names)
    tags = MP4(folder / "02 - Something.m4a") if names else {}
    expect("tagged", tags.get("\xa9alb") == ["Can't Stop"] and tags.get("aART") == ["Test Band"] and tags.get("trkn") == [(2, 0)], dict(tags or {}).keys())
    expect("cover inside the tracks", bool(tags.get("covr")))
    expect("AAC kept as it was", tags and tags.info.codec.startswith("mp4a") and job["quality"].startswith("YouTube AAC"), job["quality"])
    videos = sorted(p.name for p in (music / "Test Band").glob("*-video.mp4"))
    expect("videos filed for the artist, not the audio-only one", videos == ["Come Together-video.mp4", "Oh! Darling-video.mp4"], videos)
    expect("the message says so", "2 videos filed, 1 audio only." in job["message"], job["message"])
    expect("downloads cleared", not any(downloads_dir.rglob("*.mp4")), list(downloads_dir.rglob("*")))
    expect("one download per track", sorted(downloads()) == ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"], downloads())

    # Already have it: the album goes to import, and the videos aren't filed twice
    s.queue.jobs.clear()
    job = client.post("/api/queue/youtube", json={"album_id": ALBUM_ID}, headers=HEAD).get_json()
    job = wait(s.queue, job["id"])
    expect("owned album goes to import", job["state"] == "done" and job["where"] == "import", job["message"])
    expect("AAC quality isn't shown as a CD's", "(M4A 1" in job["message"], job["message"])
    expect("existing videos skipped", "2 already there" in job["message"] and len(list((music / "Test Band").glob("*-video.mp4"))) == 2, job["message"])

    # One track fails: the rest stay, a new link is picked up on retry, and only it is downloaded
    s.queue.jobs.clear()
    subprocess.run(["rm", "-rf", str(music / "Test Band")], check=True)
    (FAKE / "downloads.log").unlink()
    link(client, "1-2", "brokenvide0")
    job = client.post("/api/queue/youtube", json={"album_id": ALBUM_ID}, headers=HEAD).get_json()
    job = wait(s.queue, job["id"])
    expect("a failed track is named", job["state"] == "failed" and job["message"].startswith("Track 2 (“Something”): YouTube refused"), job["message"])
    expect("tracks already downloaded are kept", job.get("kept") and (Path(job["storage"]) / "aaaaaaaaaa1.mp4").exists())
    link(client, "1-2", "aaaaaaaaaa2")
    response = client.post(f"/api/queue/{job['id']}/retry", headers=HEAD)
    expect("retry accepted", response.status_code == 200, response.get_data(as_text=True))
    job = wait(s.queue, job["id"])
    expect("retry with the new link files it", job["state"] == "done" and job["where"] == "library", job["message"])
    expect("retry didn't fetch track 1 again", downloads() == ["aaaaaaaaaa1", "brokenvide0", "aaaaaaaaaa2", "aaaaaaaaaa3"], downloads())

    # Partial: one blank track, queued on purpose
    s.queue.jobs.clear()
    subprocess.run(["rm", "-rf", str(music / "Test Band")], check=True)
    link(client, "1-3", "")
    response = client.post("/api/queue/youtube", json={"album_id": ALBUM_ID, "partial": True}, headers=HEAD)
    job = wait(s.queue, response.get_json()["id"])
    expect("partial album files with a note", job["state"] == "done" and "Missing “Oh! Darling”" in job["message"], job["message"])
    expect("two tracks filed", len(list((music / job["folder"]).glob("*.m4a"))) == 2 if job.get("folder") else False)

    # Cancel mid-download kills yt-dlp
    s.queue.jobs.clear()
    subprocess.run(["rm", "-rf", str(music / "Test Band")], check=True)
    link(client, "1-3", "slowvideo00")
    job = client.post("/api/queue/youtube", json={"album_id": ALBUM_ID}, headers=HEAD).get_json()
    for _ in range(200):
        time.sleep(0.05)
        if (FAKE / "slow.pid").exists():
            break
    client.delete(f"/api/queue/{job['id']}", headers=HEAD)
    job = wait(s.queue, job["id"])
    pid = int((FAKE / "slow.pid").read_text())
    time.sleep(0.3)
    alive = Path(f"/proc/{pid}").exists() and "Z" not in Path(f"/proc/{pid}/stat").read_text().split()[2]
    expect("cancel stops the download", job["state"] == "failed" and job["message"] == "Cancelled." and not alive, job["message"])
    expect("cancelled download cleared", not job.get("storage") and not any(downloads_dir.rglob("*.mp4")))

    print(f"\n{sum(results)} of {len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
