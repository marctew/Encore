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

from encore import api, engine, split  # noqa: E402
from encore import web as server  # noqa: E402
from encore.youtube import description_chapters, description_tracks, playlist_id, youtube_id  # noqa: E402

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
    "newsingle01": {"title": "Test Band - Brand New Day (Official Video) 4K", "channel": "Test Band", "duration": 25,
                    "upload_date": "20261107"},
}
ARTIST_ID = "33333333-3333-3333-3333-333333333333"
# One video of a whole album: four tracks, a 1.5 s gap, a 0.8 s quiet spot in the middle of track 2 that
# isn't a gap between tracks, track 2 running straight into track 3 with no gap, then a 2 s gap.
WHOLE_ID = "44444444-4444-4444-4444-444444444444"
WHOLE_TITLES = ["First", "Second", "Third", "Fourth"]
WHOLE_LENGTHS = [21, 40, 26, 22]                  # MusicBrainz's lengths; the real starts are 0, 20.75, 61.5, 87.5
SEGMENTS = [[440, 20], [0, 1.5], [550, 19], [0, 0.8], [550, 20.2], [660, 25], [0, 2], [770, 21.5]]
# An album MusicBrainz doesn't have: five tracks with 2 s gaps and a 0.6 s pause inside track 2, a track
# list in the description with no timestamps, and two chapters YouTube made up.
FREE_SEGMENTS = [[300, 40], [0, 2], [350, 19], [0, 0.6], [350, 20], [0, 2], [400, 40], [0, 2], [450, 40], [0, 2], [500, 40]]
FREE_STARTS = [0, 41, 82.6, 124.6, 166.6, 207.6]
FREE_NAMES = ["Wild Rover", "Leave it Lay", "Tim Finnegans Wake", "Wild Sky", "Spanish Lady"]
# A YouTube Music album: Topic uploads, the first knowing its album, artist and year, and one gone private.
ALBUM_LIST = "OLAK5uy_testalbumplaylist0001"
BROKEN_LIST = "PLtestbrokenplaylist0001"
VIDEOS.update({
    "listtrack01": {"title": "Look Where", "channel": "Colum Test - Topic", "duration": 7, "album": "Look Where", "artist": "Colum Test",
                    "release_year": 2009, "upload_date": "20150822"},
    "listtrack02": {"title": "Beyond The Frame", "channel": "Colum Test - Topic", "duration": 8},
    "listtrack03": {"title": "Fresh Bread", "channel": "Colum Test - Topic", "duration": 9},
})
PLAYLISTS = {ALBUM_LIST: {"title": "Album - Look Where", "entries": ["listtrack01", "private", "listtrack02", "listtrack03"]},
             BROKEN_LIST: {"title": "My mix", "entries": ["listtrack01", "brokenvide0"]}}
VIDEOS.update({
    "freealbum01": {"title": '"Busking On" Full Album~Free Busker~*~', "channel": "Some Uploader", "duration": 208, "segments": FREE_SEGMENTS,
                    "upload_date": "20190304",
                    "description": "Recorded in Feb 2000~\nat a studio~\nTrack list~\n\n" + "\n".join(f"{n} *" for n in FREE_NAMES)
                                   + "\n\n*Traditional\nMore at https://example.invalid",
                    "chapters": [{"start_time": 0, "end_time": 100, "title": "<Untitled Chapter 1>"},
                                 {"start_time": 100, "end_time": 208, "title": "looking for your shoes"}]},
    "wholealbum1": {"title": "Test Band - Whole Thing (Full Album)", "channel": "Test Band", "duration": 110, "segments": SEGMENTS,
                    "description": "Tracklist:\n0:00 First\n0:21 Second\n1:01 Third (Remastered)\n1:27 Fourth\n\nThanks for listening"},
    "wholealbum2": {"title": "Test Band - Whole Thing", "channel": "Fan Uploads", "duration": 110, "segments": SEGMENTS,
                    "description": "1. First (0:21)\n2. Second (0:40)\n3. Third (0:26)\n4. Fourth (0:22)"},
})


def expect(name, ok, detail=""):
    print("PASS" if ok else "FAIL", "|", name, "|", detail)
    results.append(bool(ok))


def whole_album():
    tracks = [{"disc": 1, "number": n + 1, "title": t, "seconds": WHOLE_LENGTHS[n], "artist": "Test Band"} for n, t in enumerate(WHOLE_TITLES)]
    edition = {"id": "w1", "title": "Whole Thing", "date": "2001-01-01", "status": "Official", "label": "Fake Records", "tracks": tracks}
    return {"id": WHOLE_ID, "title": "Whole Thing", "artist": "Test Band", "artist_id": "", "year": "2001", "kind": "Album",
            "cover": "", "tracks": 4, "tracks_list": tracks, "seconds": 109, "editions": [edition], "minutes": 2, "label": "",
            "released": "2001", "discs": 1}


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
    (FAKE / "playlists.json").write_text(json.dumps(PLAYLISTS))
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
    s.brainz.album = lambda album_id, *_, **__: whole_album() if album_id == WHOLE_ID else album()
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

    # The same song filed as a single too: one tagged track in Artist/Song (Year), with the video
    s.queue.jobs.clear()
    subprocess.run(["rm", "-rf", str(music / "Test Band")], check=True)
    (FAKE / "downloads.log").unlink(missing_ok=True)
    send = lambda video, song, **wants: client.post("/api/videos/youtube", headers=HEAD,
                                                    json={"artist_id": ARTIST_ID, "video": video, "song": song, **wants})
    response = send("newsingle01", "Brand New Day", video_file=False, audio=False)
    expect("it needs the video, the song or both", response.status_code == 400, response.get_data(as_text=True))
    response = send("newsingle01", "Brand New Day", audio=True)
    expect("a single is queued", response.status_code == 200 and response.get_json()["kind"] == "single", response.get_data(as_text=True))
    job = wait(s.queue, response.get_json()["id"])
    expect("the single is filed", job["state"] == "done" and job["where"] == "library" and job["folder"] == "Test Band/Brand New Day (2026)",
           (job["message"], job.get("folder")))
    folder = music / "Test Band" / "Brand New Day (2026)"
    names = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
    expect("one track and a cover", names == ["01 - Brand New Day.m4a", "cover.jpg"], names)
    tags = MP4(folder / "01 - Brand New Day.m4a") if names else {}
    expect("tagged as its own single", tags.get("\xa9alb") == ["Brand New Day"] and tags.get("\xa9nam") == ["Brand New Day"]
           and tags.get("aART") == ["Test Band"] and tags.get("trkn") == [(1, 0)] and tags.get("\xa9day") == ["2026"], dict(tags or {}))
    expect("the thumbnail is its cover", bool(tags.get("covr")))
    expect("the year is the upload's, from a fresh lookup", job["year"] == "2026", job.get("year"))
    expect("its video filed beside it", (music / "Test Band" / "Brand New Day-video.mp4").exists() and "Video filed." in job["message"], job["message"])
    expect("quality says YouTube AAC", job["quality"].startswith("YouTube AAC"), job["quality"])
    expect("the single's download is cleared", downloads() == ["newsingle01"] and not any(downloads_dir.rglob("*.mp4")))
    job = wait(s.queue, send("newsingle01", "Brand New Day", video_file=False, audio=True).get_json()["id"])
    expect("a single you have goes to import to compare", job["state"] == "done" and job["where"] == "import"
           and "You already have Brand New Day (2026)" in job["message"], job["message"])
    expect("song only leaves the video alone", "Video" not in job["message"], job["message"])
    find("https://youtu.be/aaaaaaaaaa2")
    job = wait(s.queue, send("aaaaaaaaaa2", "Something", video_file=False, audio=True).get_json()["id"])
    year = time.strftime("%Y")
    expect("a Topic upload makes a song with no video", job["state"] == "done" and job["folder"] == f"Test Band/Something ({year})"
           and not (music / "Test Band" / "Something-video.mp4").exists(), (job["message"], job.get("folder")))

    check_whole(client, s, music, downloads_dir)
    check_free(client, s, music, downloads_dir)
    check_playlist(client, s, music, downloads_dir)

    print(f"\n{sum(results)} of {len(results)} passed")
    return 0 if all(results) else 1


def near(value, wanted, slack=0.35):
    return abs(value - wanted) <= slack


def check_whole(client, s, music, downloads_dir):
    """One video of the whole album: found, checked by a person, cut and filed."""
    starts = description_chapters("Tracklist:\n0:00 First\n0:21 Second\n1:01 Third\n1:27 Fourth\nFollow us at 10:30 and 11:00")
    expect("timestamps are read from the description", [c["start"] for c in starts] == [0, 21, 61, 87]
           and [c["title"] for c in starts] == ["First", "Second", "Third", "Fourth"], starts)
    lengths = description_chapters("1. First (3:21)\n2. Second (4:40)\n3. Third (2:26)")
    expect("a list of track lengths isn't taken for starts", lengths == [], lengths)
    expect("numbered and bracketed lines are read", [c["title"] for c in description_chapters(
        "01. One - 0:00\n02. Two - 3:10\n[6:20] Three\n1:02:03 – Four")] == ["One", "Two", "Three", "Four"])

    s.queue.jobs.clear()
    subprocess.run(["rm", "-rf", str(music / "Test Band")], check=True)
    (FAKE / "downloads.log").unlink(missing_ok=True)
    whole = lambda url: client.post(f"/api/album/{WHOLE_ID}/youtube/whole", json={"url": url}, headers=HEAD)  # noqa: E731
    queue_it = lambda: client.post("/api/queue/youtube/whole", json={"album_id": WHOLE_ID}, headers=HEAD)  # noqa: E731
    response = queue_it()
    expect("queueing needs the video first", response.status_code == 409, response.get_data(as_text=True))
    expect("a link to another site is refused", whole("https://evil.example/watch?v=wholealbum1").status_code == 400)
    built = whole("https://www.youtube.com/watch?v=wholealbum1").get_json()
    video = built.get("whole") or {}
    expect("the album's video is looked up with its timestamps", video.get("id") == "wholealbum1" and len(video.get("chapters", [])) == 4
           and video.get("fit") == "good" and not video.get("notes"), video)
    expect("the per-track links are left alone", all(not t["video"] for t in built["tracks"]))
    found = client.post(f"/api/album/{WHOLE_ID}/youtube/whole/find", headers=HEAD).get_json()["results"]
    expect("Find scores videos against the whole album's length", found and found[0]["id"] in ("wholealbum1", "wholealbum2"),
           [(v["id"], v["score"]) for v in found])

    # Downloaded, looked at, and then it waits
    job = queue_it().get_json()
    expect("can't queue it twice", queue_it().status_code == 409)
    job = wait(s.queue, job["id"], ("review", "failed"))
    expect("it waits for the tracks to be checked", job["state"] == "review" and job.get("kept"), job["message"])
    expect("only the sound was downloaded", downloads() == ["wholealbum1"] and (Path(s.queue.get(job["id"])["storage"]) / "album.m4a").exists())
    expect("nothing is filed yet", not (music / "Test Band").exists())
    with client.application.test_request_context():
        status = api.status_body()
    plan = client.get(f"/api/queue/{job['id']}/split").get_json()
    cuts = plan["cuts"]
    expect("starts come from the timestamps", plan["source"] == "chapters" and len(cuts) == 5 and cuts[0] == 0 and near(cuts[4], 110, 0.3), cuts)
    expect("each start moves to the nearest gap", near(cuts[1], 20.75) and near(cuts[3], 87.5), cuts)
    expect("a timestamp with no gap stays put and says so", cuts[2] == 61 and plan["marks"][2] == {"from": "chapter", "gap": False, "moved": 0}
           and plan["marks"][1]["gap"] and plan["marks"][3]["gap"], plan["marks"])
    expect("the quiet spot inside a track is a gap the page can snap to, but wasn't used",
           any(near(g, 40.9, 0.5) for g in plan["gaps"]) and not any(near(c, 40.9, 1) for c in cuts), plan["gaps"])
    expect("the timestamps' own names come along", [t["chapter"] for t in plan["tracks"]] == ["First", "Second", "Third (Remastered)", "Fourth"])
    expect("a waveform comes with it", 100 <= len(plan["peaks"]) <= split.BARS and max(plan["peaks"]) > 50 and min(plan["peaks"]) == 0)
    audio = client.get(f"/api/queue/{job['id']}/split/audio", headers={"Range": "bytes=0-99"})
    expect("its sound can be played in pieces", audio.status_code == 206 and len(audio.data) == 100 and audio.mimetype == "audio/mp4", audio.status_code)
    queue_rows = client.get("/api/queue").get_json()["jobs"]
    expect("the queue doesn't send the plan around", "storage" not in queue_rows[0] and queue_rows[0]["state"] == "review")

    send = lambda body: client.post(f"/api/queue/{job['id']}/split", json=body, headers=HEAD)  # noqa: E731
    expect("too few starts are refused", send({"cuts": cuts[:3]}).status_code == 400)
    expect("starts out of order are refused", send({"cuts": [0, 60, 30, 87.5, 110]}).status_code == 400)
    expect("a start past the end is refused", send({"cuts": [0, 20, 60, 87, 200]}).status_code == 400)
    expect("starts that aren't numbers are refused", send({"cuts": [0, "x", 60, 87, 110]}).status_code == 400)
    fixed = [cuts[0], cuts[1], 61.5, cuts[3], cuts[4]]
    response = send({"cuts": fixed})
    expect("a nudge is saved", response.status_code == 200 and client.get(f"/api/queue/{job['id']}/split").get_json()["cuts"] == fixed
           and s.queue.get(job["id"])["state"] == "review", response.get_data(as_text=True))
    expect("Encore's own guess is kept beside it", client.get(f"/api/queue/{job['id']}/split").get_json()["found"] == cuts)

    # A failure after the check goes back to it, without downloading again
    real_cut = split.cut
    split.cut = lambda *_: False
    send({"cuts": fixed, "file": True})
    failed = wait(s.queue, job["id"])
    split.cut = real_cut
    expect("a track that won't cut fails the job, keeping the download", failed["state"] == "failed" and "Track 1" in failed["message"]
           and (Path(failed["storage"]) / "album.m4a").exists(), failed["message"])
    client.post(f"/api/queue/{job['id']}/retry", headers=HEAD)
    job = wait(s.queue, job["id"], ("review", "failed"))
    expect("retry goes back to the check with the nudges kept", job["state"] == "review" and downloads() == ["wholealbum1"]
           and client.get(f"/api/queue/{job['id']}/split").get_json()["cuts"] == fixed, (job["state"], downloads()))

    # Confirmed: cut, tagged and filed
    response = send({"cuts": fixed, "file": True})
    expect("filing is accepted", response.status_code == 200 and response.get_json()["job"]["state"] == "queued", response.get_data(as_text=True))
    expect("it can't be filed twice", send({"cuts": fixed, "file": True}).status_code == 409)
    job = wait(s.queue, job["id"])
    expect("the album is filed", job["state"] == "done" and job["where"] == "library", job["message"])
    folder = music / job["folder"] if job.get("folder") else music / "nowhere"
    names = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
    expect("one file for each track", names == ["01 - First.m4a", "02 - Second.m4a", "03 - Third.m4a", "04 - Fourth.m4a", "cover.jpg"], names)
    seconds = [MP4(folder / n).info.length for n in names if n.endswith(".m4a")]
    wanted = [b - a for a, b in zip(fixed, fixed[1:])]
    expect("each is cut where it was checked", len(seconds) == 4 and all(near(a, b, 0.15) for a, b in zip(seconds, wanted)),
           [round(x, 2) for x in seconds])
    tags = MP4(folder / "03 - Third.m4a") if names else {}
    expect("tagged from MusicBrainz", tags.get("\xa9alb") == ["Whole Thing"] and tags.get("trkn") == [(3, 0)] and bool(tags.get("covr")))
    expect("still AAC, not re-encoded", tags and tags.info.codec.startswith("mp4a") and job["quality"].startswith("YouTube AAC"), job["quality"])
    expect("no music videos filed", not list((music / "Test Band").glob("*-video.mp4")))
    expect("the download is cleared", not any(downloads_dir.rglob("album.m4a")), list(downloads_dir.rglob("*")))
    expect("the status asked for the check while it waited", status["headline"] == "1 album to check"
           and {"key": "to_check", "label": "To check", "value": 1, "tone": "warning"} in status["counts"], status)

    # No timestamps that fit: starts from the track lengths
    s.queue.jobs.clear()
    subprocess.run(["rm", "-rf", str(music / "Test Band")], check=True)
    whole("wholealbum2")
    job = wait(s.queue, queue_it().get_json()["id"], ("review", "failed"))
    plan = client.get(f"/api/queue/{job['id']}/split").get_json() if job["state"] == "review" else {"cuts": [], "marks": []}
    cuts = plan["cuts"]
    expect("with no timestamps, starts come from the lengths", plan.get("source") == "lengths" and len(cuts) == 5, (job["message"], cuts))
    expect("and still find the gaps", len(cuts) == 5 and near(cuts[1], 20.75) and near(cuts[3], 87.5), cuts)
    expect("the join with no gap is a marked guess", len(cuts) == 5 and plan["marks"][2]["from"] == "length" and not plan["marks"][2]["gap"]
           and near(cuts[2], 60.75, 0.5), plan["marks"])
    storage = Path(s.queue.get(job["id"])["storage"])
    client.delete(f"/api/queue/{job['id']}", headers=HEAD)
    expect("clearing it removes the download", not storage.exists() and not s.queue.get(job["id"]))

    # The plan on its own: a gapless album, and a video much longer than the album
    levels = [-20.0] * 600
    plan = split.plan(levels, [20, 20, 20], [])
    expect("gapless: starts fall on the track lengths, all marked", plan["cuts"] == [0.0, 20.0, 40.0, 60.0]
           and not any(m["gap"] for m in plan["marks"][1:]), plan["cuts"])
    plan = split.plan([-20.0] * 900, [20, 20, 20], [{"start": 0, "title": "a"}, {"start": 30, "title": "b"}])
    expect("a mismatched chapter count and length are warned about", len(plan["warnings"]) == 2 and plan["source"] == "lengths", plan["warnings"])


def check_free(client, s, music, downloads_dir):
    """One video of an album MusicBrainz doesn't have: named by hand, split at the gaps, checked and filed."""
    real = ("Recorded in Feb 2000~\nat Satellite Studios~\nTrack list~\n\nWild Rover *\nLeave it Lay **\nThe Bands Played Waltzin' Matilda ***\n"
            "Galway Bay *\n\n*Traditional, arr Deane\n** by Alan Deane")
    expect("a track list without timestamps is read", description_tracks(real) == ["Wild Rover", "Leave it Lay", "The Bands Played Waltzin' Matilda", "Galway Bay"],
           description_tracks(real))
    expect("so is a numbered list with lengths", description_tracks("1. One (3:21)\n2. Two (4:40)\n3. Three [2:26]\nSubscribe!") == ["One", "Two", "Three"])
    expect("prose isn't a track list", description_tracks("Some text\nmore text\nand more\nhttps://x.invalid") == [])
    guess = server.album_guess({"title": '"Celtic Journey" Full Album~Belfast Busker~Alan Deane~*~', "channel": "Keltic Rambler",
                                "chapters": [], "tracklist": ["A", "B", "C"], "recorded": "2000"})
    expect("the album is guessed from a quoted title", guess == {"artist": "Keltic Rambler", "title": "Celtic Journey", "year": "2000",
                                                                "tracks": ["A", "B", "C"]}, guess)
    guess = server.album_guess({"title": "Test Band - Night Songs (Full Album)", "channel": "Uploads", "chapters": [], "tracklist": [], "recorded": ""})
    expect("and from Artist - Album", guess["artist"] == "Test Band" and guess["title"] == "Night Songs", guess)

    s.queue.jobs.clear()
    subprocess.run(["rm", "-rf", str(music / "Free Busker")], check=True)
    (FAKE / "downloads.log").unlink(missing_ok=True)
    expect("a link is needed", client.post("/api/youtube/album", json={"url": "Busking On"}, headers=HEAD).status_code == 400)
    found = client.post("/api/youtube/album", json={"url": "https://youtu.be/freealbum01"}, headers=HEAD).get_json()
    expect("the video is looked up with Encore's guess", found["guess"] == {"artist": "Some Uploader", "title": "Busking On", "year": "2000",
                                                                         "tracks": FREE_NAMES} and found["job"] is None, found.get("guess"))
    expect("YouTube's made-up chapter has no name", [c["title"] for c in found["video"]["chapters"]] == ["", "looking for your shoes"])
    queue_it = lambda **body: client.post("/api/queue/youtube/free", headers=HEAD, json={"video": "freealbum01", "artist": "Free Busker",  # noqa: E731
                                                                                        "title": "Busking On", "year": "2000", "tracks": FREE_NAMES, **body})
    expect("the artist is needed", queue_it(artist=" ").status_code == 400)
    expect("a year must be a year", queue_it(year="00").status_code == 400)
    expect("one track is refused", queue_it(tracks=["Only"]).status_code == 400)
    expect("a video never looked up is refused", queue_it(video="aaaaaaaaaa9").status_code == 409)

    job = queue_it().get_json()
    expect("can't queue it twice", queue_it().status_code == 409)
    job = wait(s.queue, job["id"], ("review", "failed"))
    expect("it waits to be checked", job["state"] == "review", job["message"])
    plan = client.get(f"/api/queue/{job['id']}/split").get_json()
    cuts = plan.get("cuts", [])
    expect("the five longest gaps are the starts", plan.get("free") and plan["source"] == "gaps" and len(cuts) == 6
           and all(near(a, b) for a, b in zip(cuts, FREE_STARTS)), cuts)
    expect("the pause inside a track isn't one", not any(near(c, 61.8, 1) for c in cuts))
    expect("tracks are named from the list", plan["titles"] == FREE_NAMES and [t["title"] for t in plan["tracks"]] == FREE_NAMES)
    expect("chapters that don't fit are warned about", any("2 chapters" in w for w in plan["warnings"]), plan["warnings"])
    expect("each start found at a gap", all(m["gap"] for m in plan["marks"][1:]), plan["marks"])

    send = lambda body: client.post(f"/api/queue/{job['id']}/split", json=body, headers=HEAD)  # noqa: E731
    expect("names must match the tracks", send({"cuts": cuts, "titles": FREE_NAMES[:4]}).status_code == 400)
    joined = [cuts[0], cuts[2], cuts[3], cuts[4], cuts[5]]
    response = send({"cuts": joined, "titles": ["Rover and Lay", *FREE_NAMES[2:]]})
    expect("two tracks can be joined", response.status_code == 200 and response.get_json()["titles"][0] == "Rover and Lay", response.get_data(as_text=True))
    split_again = [cuts[0], cuts[1], cuts[2], cuts[3], 150.0, cuts[4], cuts[5]]
    response = send({"cuts": split_again, "titles": [*FREE_NAMES[:4], "", "Spanish Lady"]})
    plan = client.get(f"/api/queue/{job['id']}/split").get_json()
    expect("a track can be added, and a blank name becomes Track N", plan["titles"][4] == "Track 5" and len(plan["cuts"]) == 7, plan["titles"])
    expect("an added start is marked as yours, the others keep Encore's marks",
           plan["marks"][4]["from"] == "you" and plan["marks"][3]["from"] == "gap" and len(plan["marks"]) == 6, plan["marks"])
    expect("Encore's own plan is kept for Undo", plan["found"] == cuts and plan["found_titles"] == FREE_NAMES)

    titles = [*FREE_NAMES[:3], "Wild Sky", "Spanish Lady"]
    response = send({"cuts": cuts, "titles": titles, "file": True})
    expect("filing is accepted", response.status_code == 200, response.get_data(as_text=True))
    job = wait(s.queue, job["id"])
    expect("the album is filed as Artist/Album (Year)", job["state"] == "done" and job.get("folder") == "Free Busker/Busking On (2000)",
           (job["message"], job.get("folder")))
    folder = music / "Free Busker" / "Busking On (2000)"
    names = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
    expect("named from what was typed", names == [f"0{n + 1} - {t}.m4a" for n, t in enumerate(titles)] + ["cover.jpg"], names)
    tags = MP4(folder / "03 - Tim Finnegans Wake.m4a") if names else {}
    expect("tagged from what was typed", tags.get("\xa9alb") == ["Busking On"] and tags.get("\xa9ART") == ["Free Busker"]
           and tags.get("aART") == ["Free Busker"] and tags.get("trkn") == [(3, 5)] and tags.get("\xa9day") == ["2000"]
           and tags.get("\xa9nam") == ["Tim Finnegans Wake"], dict(tags or {}).keys())
    expect("the thumbnail is the cover", bool(tags.get("covr")))
    seconds = [MP4(folder / n).info.length for n in names if n.endswith(".m4a")]
    expect("each cut where it was checked", len(seconds) == 5 and all(near(a, b - c, 0.15) for a, b, c in zip(seconds, cuts[1:], cuts)),
           [round(x, 1) for x in seconds])
    expect("the download is cleared", not any(downloads_dir.rglob("album.m4a")))

    # No track list: the clear gaps decide
    s.queue.jobs.clear()
    subprocess.run(["rm", "-rf", str(music / "Free Busker")], check=True)
    job = wait(s.queue, queue_it(tracks=[], year="").get_json()["id"], ("review", "failed"))
    plan = client.get(f"/api/queue/{job['id']}/split").get_json() if job["state"] == "review" else {"cuts": [], "titles": [], "warnings": []}
    expect("with no list, every clear gap starts a track", len(plan["cuts"]) == 6 and all(near(a, b) for a, b in zip(plan["cuts"], FREE_STARTS)),
           plan["cuts"])
    expect("and they're named Track 1 to 5 for now", plan["titles"] == [f"Track {n}" for n in range(1, 6)], plan["titles"])
    expect("and it says to check the count", any("found 5 tracks" in w for w in plan["warnings"]), plan["warnings"])
    client.delete(f"/api/queue/{job['id']}", headers=HEAD)

    # The plan on its own: too few gaps for the tracks
    levels = [-20.0] * 900
    levels[300:320] = [-90.0] * 20
    plan = split.plan_free(levels, 3, [])
    expect("too few gaps: the rest go halfway and are marked", plan["cuts"][1] == 31.0 and len(plan["cuts"]) == 4
           and [m["gap"] for m in plan["marks"][1:]] == [True, False] and "put halfway" in plan["warnings"][0], (plan["cuts"], plan["warnings"]))


def check_playlist(client, s, music, downloads_dir):
    """A playlist that's an album MusicBrainz doesn't have: each video one track, named by hand."""
    links = {f"https://www.youtube.com/playlist?list={ALBUM_LIST}": ALBUM_LIST,
             f"https://music.youtube.com/playlist?list={ALBUM_LIST}&si=abc": ALBUM_LIST,
             f"https://www.youtube.com/watch?v=listtrack01&list={ALBUM_LIST}": ALBUM_LIST,
             "https://www.youtube.com/watch?v=listtrack01&list=PLsomeonesmixplaylist01": None,
             f"https://evil.example/playlist?list={ALBUM_LIST}": None,
             "https://www.youtube.com/playlist?list=PL;rm -rf": None, ALBUM_LIST: ALBUM_LIST}
    read = {link: playlist_id(link) for link in links}
    expect("playlist links are read, and a mix in a watch link isn't taken for an album", read == links, read)

    s.queue.jobs.clear()
    (FAKE / "downloads.log").unlink(missing_ok=True)
    found = client.post("/api/youtube/album", json={"url": f"https://music.youtube.com/playlist?list={ALBUM_LIST}"}, headers=HEAD).get_json()
    playlist = found.get("playlist") or {}
    expect("the playlist is looked up, leaving out the private video", [e["id"] for e in playlist.get("entries", [])]
           == ["listtrack01", "listtrack02", "listtrack03"] and playlist.get("missing") == 1, playlist)
    expect("the album, artist and year come from YouTube Music", found.get("guess") == {"artist": "Colum Test", "title": "Look Where",
           "year": "2009", "tracks": ["Look Where", "Beyond The Frame", "Fresh Bread"]}, found.get("guess"))
    missing = client.post("/api/youtube/album", json={"url": "https://www.youtube.com/playlist?list=PLnosuchplaylist000"}, headers=HEAD)
    expect("a missing playlist says so", missing.status_code == 503, missing.get_data(as_text=True))

    queue_it = lambda tracks, **body: client.post("/api/queue/youtube/free", headers=HEAD, json={  # noqa: E731
        "playlist": ALBUM_LIST, "artist": "Colum Test", "title": "Look Where", "year": "2009", "tracks": tracks, **body})
    expect("no tracks is refused", queue_it([]).status_code == 400)
    expect("a video that isn't known is refused", queue_it([{"video": "zzzzzzzzzzz", "title": "X"}]).status_code == 409)
    expect("the same video twice is refused", queue_it([{"video": "listtrack01", "title": "A"}, {"video": "listtrack01", "title": "B"}]).status_code == 409)
    expect("a bad playlist id is refused", queue_it([{"video": "listtrack01", "title": "A"}], playlist="nope").status_code == 400)
    kept = [{"video": "listtrack01", "title": "Look Where"}, {"video": "listtrack03", "title": "Fresh Bread (Live)"}]
    job = queue_it(kept).get_json()
    expect("can't queue it twice", queue_it(kept).status_code == 409)
    job = wait(s.queue, job["id"])
    expect("the album is filed as Artist/Album (Year)", job["state"] == "done" and job.get("folder") == "Colum Test/Look Where (2009)",
           (job["message"], job.get("folder")))
    folder = music / "Colum Test" / "Look Where (2009)"
    names = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
    expect("tracks numbered in order, with the left-out one gone", names == ["01 - Look Where.m4a", "02 - Fresh Bread (Live).m4a", "cover.jpg"], names)
    tags = MP4(folder / "02 - Fresh Bread (Live).m4a") if names else {}
    expect("tagged from what was typed", tags.get("\xa9alb") == ["Look Where"] and tags.get("aART") == ["Colum Test"]
           and tags.get("trkn") == [(2, 2)] and tags.get("\xa9day") == ["2009"] and bool(tags.get("covr")), dict(tags or {}).keys())
    expect("each track's length is its video's", names and near(MP4(folder / names[1]).info.length, 9, 0.2))
    expect("only the sound of the kept videos was downloaded", downloads() == ["listtrack01", "listtrack03"]
           and not any(downloads_dir.rglob("*.m4a")), downloads())
    expect("it's marked as from YouTube with no MusicBrainz cover", job["kind"] == "youtube_list" and job["quality"].startswith("YouTube AAC"))

    # A video that won't download: the rest stay, and a retry doesn't fetch them again
    s.queue.jobs.clear()
    (FAKE / "downloads.log").unlink(missing_ok=True)
    client.post("/api/youtube/album", json={"url": f"https://www.youtube.com/playlist?list={BROKEN_LIST}"}, headers=HEAD)
    body = {"playlist": BROKEN_LIST, "artist": "Mixed", "title": "Mix", "year": "",
            "tracks": [{"video": "listtrack01", "title": "One"}, {"video": "brokenvide0", "title": "Two"}]}
    job = wait(s.queue, client.post("/api/queue/youtube/free", headers=HEAD, json=body).get_json()["id"])
    expect("the track that failed is named", job["state"] == "failed" and job["message"].startswith("Track 2 (“Two”): YouTube refused"), job["message"])
    expect("the other track's download is kept", job.get("kept") and (Path(s.queue.get(job["id"])["storage"]) / "listtrack01.m4a").exists())
    client.post(f"/api/queue/{job['id']}/retry", headers=HEAD)
    job = wait(s.queue, job["id"])
    expect("a retry only fetches what's missing", downloads() == ["listtrack01", "brokenvide0", "brokenvide0"], downloads())
    client.delete(f"/api/queue/{job['id']}", headers=HEAD)


if __name__ == "__main__":
    sys.exit(main())
