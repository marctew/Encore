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
    "aaaaaaaaaa5": {"title": "Test Band - Something (Official Video)", "channel": "Test Band", "duration": 9},
    "slowvideo00": {"title": "Test Band - Oh! Darling", "channel": "Test Band", "duration": 10, "slow": True},
    "newsingle01": {"title": "Test Band - Brand New Day (Official Video) 4K", "channel": "Test Band", "duration": 25},
}
ARTIST_ID = "33333333-3333-3333-3333-333333333333"


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
    expect("a missing video says so", status == 503 and "removed or made private" in body["error"], body)
    status, body = link(client, "9-9", "aaaaaaaaaa1")
    expect("a track that isn't there is refused", status == 404, body)
    status, body = link(client, "1-1", "https://youtu.be/aaaaaaaaaa1")
    expect("a good link is looked up", status == 200 and body["video"]["title"].startswith("Test Band - Come") and body["video"]["fit"] == "good", body)

    found = client.post(f"/api/album/{ALBUM_ID}/youtube/find", json={"key": "1-2"}, headers=HEAD).get_json()["results"]
    ids = [v["id"] for v in found]
    expect("Find puts the Topic upload first", ids[0] == "aaaaaaaaaa2" and found[0]["audio_only"], ids)
    live = [v for v in found if v["id"] == "aaaaaaaaaa4"]
    expect("Find puts the live fan upload last, if at all", (not live or (ids[-1] == "aaaaaaaaaa4" and live[0]["fit"] == "off"))
           and "aaaaaaaaaa4" != ids[0], [(v["id"], v["score"]) for v in found])

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
    expect("the message says so", "2 videos filed, no video from 1 audio-only upload." in job["message"], job["message"])
    expect("downloads cleared", not any(downloads_dir.rglob("*.mp4")), list(downloads_dir.rglob("*")))
    expect("one download per track", sorted(downloads()) == ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"], downloads())

    # Already have it: the album goes to import, and the videos aren't filed twice
    s.queue.jobs.clear()
    job = client.post("/api/queue/youtube", json={"album_id": ALBUM_ID}, headers=HEAD).get_json()
    job = wait(s.queue, job["id"])
    expect("owned album goes to import", job["state"] == "done" and job["where"] == "import", job["message"])
    held = sorted(p.name for p in (imports / job["folder"]).iterdir()) if job.get("folder") else []
    expect("held album is named like any download", job["folder"].startswith("Test Band - Can't Stop [")
           and "01 - Come Together.m4a" in held and "cover.jpg" in held, (job["folder"], held))
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

    # A Usenet download that's missing a track, filled from YouTube
    s.queue.jobs.clear()
    subprocess.run(["rm", "-rf", str(music / "Test Band")], check=True)
    release = downloads_dir / "Test Band - Can't Stop [usenet]"
    release.mkdir()
    for number, seconds in ((1, 8), (3, 10)):
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", f"sine=frequency={200 * number}:duration={seconds}",
                        "-ar", "44100", "-sample_fmt", "s16", str(release / f"{number:02d} - {TITLES[number - 1]}.flac")], check=True)
    usenet = {"title": "Can't Stop", "artist": "Test Band", "year": "1969", "tracks": 3, "cover": "", "album_id": ALBUM_ID,
              "id": "usenet000001", "state": "queued", "percent": 0, "message": "", "fix_artist": True, "folder": "", "quality": "",
              "added": time.time(), "partial": False, "any_format": False, "reuse": True, "storage": str(release),
              "source": {"indexer": "Fake", "guid": "g", "title": "Test Band - Can't Stop FLAC", "size": 10**7, "key": "k"}}
    with s.queue.lock:
        s.queue.jobs.append(usenet)
    s.queue.wake.set()
    job = wait(s.queue, usenet["id"])
    expect("a missing track waits", job["state"] == "failed" and job.get("can_partial") and [m["key"] for m in job.get("missing", [])] == ["1-2"], job["message"])
    link(client, "1-2", "")
    gaps = client.get(f"/api/album/{ALBUM_ID}/youtube?job={job['id']}").get_json()
    expect("Fill from YouTube lists only the gap", [t["title"] for t in gaps["tracks"]] == ["Something"] and gaps["fill"]["have"] == 2, gaps)
    response = client.post(f"/api/queue/{job['id']}/fill", headers=HEAD)
    expect("filling needs a link first", response.status_code == 409, response.get_data(as_text=True))
    link(client, "1-2", "aaaaaaaaaa5")
    (FAKE / "downloads.log").unlink(missing_ok=True)
    response = client.post(f"/api/queue/{job['id']}/fill", headers=HEAD)
    expect("fill accepted", response.status_code == 200, response.get_data(as_text=True))
    job = wait(s.queue, job["id"])
    expect("filled album files", job["state"] == "done" and job["where"] == "library", job["message"])
    names = sorted(p.name for p in (music / job["folder"]).iterdir()) if job.get("folder") else []
    expect("FLAC and AAC side by side", names == ["01 - Come Together.flac", "02 - Something.m4a", "03 - Oh! Darling.flac"], names)
    tags = MP4(music / job["folder"] / "02 - Something.m4a") if names else {}
    expect("the filled track is tagged", tags.get("\xa9alb") == ["Can't Stop"] and tags.get("trkn") == [(2, 0)], dict(tags or {}).keys())
    expect("the message says what came from where", "1 track from YouTube (AAC)." in job["message"] and "Missing" not in job["message"]
           and "1 video filed." in job["message"], job["message"])
    expect("only the gap was downloaded", downloads() == ["aaaaaaaaaa5"], downloads())
    expect("its video filed", (music / "Test Band" / "Something-video.mp4").exists())

    # One music video for a song MusicBrainz doesn't list yet, from the artist page
    s.queue.jobs.clear()
    subprocess.run(["rm", "-rf", str(music / "Test Band")], check=True)
    s.store.save_artist({"id": ARTIST_ID, "name": "Test Band"})
    find = lambda q: client.post(f"/api/artist/{ARTIST_ID}/youtube", json={"q": q}, headers=HEAD)
    response = find("Brand New Day")
    found = response.get_json().get("results") or []
    expect("a song name searches YouTube, the artist's own video first", response.status_code == 200 and found
           and found[0]["id"] == "newsingle01" and found[0]["song"] == "Brand New Day", [(v["id"], v["score"], v["song"]) for v in found])
    expect("a Topic upload isn't offered first", "aaaaaaaaaa2" not in [v["id"] for v in found[:1]])
    topic = (find("https://youtu.be/aaaaaaaaaa2").get_json().get("results") or [{}])[0]
    expect("a Topic upload is marked as no real video, its title taken as the song", topic.get("audio_only")
           and topic["notes"][0].startswith("Audio over") and topic["song"] == "Something", topic)
    response = find("https://www.youtube.com/watch?v=newsingle01")
    found = response.get_json().get("results") or []
    expect("a pasted link gives that video", response.status_code == 200 and [v["id"] for v in found] == ["newsingle01"], found)
    response = find("https://evil.example/watch?v=newsingle01")
    expect("a link to another site is refused here too", response.status_code == 400, response.get_data(as_text=True))
    response = find("Starburster")
    expect("an 11-letter song name is searched, not taken as a video id", response.status_code == 200 and len(response.get_json()["results"]) > 1)
    expect("a missing artist is refused", client.post("/api/artist/44444444-4444-4444-4444-444444444444/youtube", json={"q": "x"},
                                                      headers=HEAD).status_code == 404)
    queue_single = lambda video, song: client.post("/api/videos/youtube", json={"artist_id": ARTIST_ID, "video": video, "song": song}, headers=HEAD)
    response = queue_single("newsingle01", "  ")
    expect("a video needs a song name", response.status_code == 400, response.get_data(as_text=True))
    response = queue_single("zzzzzzzzzz9", "Brand New Day")
    expect("a video Encore hasn't looked up is refused", response.status_code == 409, response.get_data(as_text=True))
    (FAKE / "downloads.log").unlink(missing_ok=True)
    response = queue_single("newsingle01", "Brand New Day")
    expect("the video is queued", response.status_code == 200 and response.get_json()["kind"] == "video", response.get_data(as_text=True))
    again = queue_single("newsingle01", "Brand New Day")
    expect("can't queue the same video twice", again.status_code == 409)
    job = wait(s.queue, response.get_json()["id"])
    expect("the video is filed", job["state"] == "done" and job["where"] == "library" and job["folder"] == "Test Band/Brand New Day-video.mp4",
           (job["message"], job.get("folder")))
    expect("it's where Plex looks for music videos", (music / "Test Band" / "Brand New Day-video.mp4").exists())
    expect("its quality is the video's", job["quality"].startswith("90p · 0:25"), job["quality"])
    expect("the video download is cleared", downloads() == ["newsingle01"] and not any(downloads_dir.rglob("*.mp4")), list(downloads_dir.rglob("*")))
    job = wait(s.queue, queue_single("newsingle01", "Brand New Day").get_json()["id"])
    expect("a video already there isn't overwritten", job["state"] == "failed" and "already have" in job["message"], job["message"])
    find("https://youtu.be/aaaaaaaaaa1")
    job = wait(s.queue, queue_single("aaaaaaaaaa1", "Come Together").get_json()["id"])
    expect("a clip too short to be a video fails", job["state"] == "failed" and "isn't a playable video" in job["message"], job["message"])

    print(f"\n{sum(results)} of {len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
