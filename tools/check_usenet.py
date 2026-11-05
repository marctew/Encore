"""Runs whole jobs against a fake indexer and a fake SABnzbd in throwaway folders.

Audio is generated with ffmpeg. Touches nothing real and sends nothing anywhere.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("ENCORE_DATA", tempfile.mkdtemp(prefix="encore-check-data-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mutagen
from flask import Flask, Response, jsonify, request
from mutagen.flac import FLAC
from werkzeug.serving import make_server

from encore import engine as server
from encore import match, sources
from encore import web as encore_web
from encore.store import Store

TONES = Path(tempfile.mkdtemp(prefix="encore-tones-"))
SOURCE = [TONES / f"{n}.flac" for n in range(4)]
for number, path in enumerate(SOURCE):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", f"sine=frequency={300 + number * 100}:duration={8 + number}",
                    "-ar", "44100", "-sample_fmt", "s16", str(path)], check=True)
SECRET = "SECRETKEY123"
results, seen = [], []
ALBUM_ID = "11111111-1111-1111-1111-111111111111"
titles = ["Come Together", "Something", "Maxwell's Silver Hammer", "Oh! Darling"]
RSS = """<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>{}</channel></rss>"""
ITEM = """<item><title>{title}</title><guid>https://x/details/{guid}</guid><pubDate>Mon, 02 Nov 2026 10:00:00 +0000</pubDate>
<newznab:attr name="size" value="{size}"/><newznab:attr name="category" value="3040"/><newznab:attr name="grabs" value="3"/></item>"""


def expect(name, ok, detail=""):
    print("PASS" if ok else "FAIL", "|", name, "|", detail)
    results.append(bool(ok))


class Fakes:
    def __init__(self):
        self.plan = {}
        self.hits = {}
        self.deleted = []
        self.cats = ["*"]

    def indexer(self):
        query = request.args
        seen.append(dict(query))
        if query.get("apikey") != SECRET:
            return Response(status=403)
        if query["t"] == "get":
            return Response(b"<nzb></nzb>" if query["id"] != "nope" else b'<error code="429" description="Limit reached"/>')
        if query["t"] == "caps":
            return Response("<caps></caps>")
        items = ""
        if query["q"] == "Test Band Can t Stop":
            items = ITEM.format(title="Test Band - Can.t Stop (1969) FLAC", guid="good", size=6 * 10**7)
        return Response(RSS.format(items), content_type="application/xml")

    def sab(self):
        query = request.args
        mode = query["mode"]
        if query.get("apikey") != SECRET:
            return jsonify({"status": False, "error": "API Key Incorrect"})
        if mode == "version":
            return jsonify({"version": "4.0"})
        if mode == "get_cats":
            return jsonify({"categories": self.cats})
        if mode == "set_config":
            self.cats.append(query["name"])
            return jsonify({"status": True})
        if mode == "addfile":
            assert request.files.get("name"), "the NZB arrives as a file"
            name = query["nzbname"]
            nzo = f"SABnzbd_nzo_{len(self.hits)}"
            self.hits[nzo] = name
            return jsonify({"status": True, "nzo_ids": [nzo]})
        if query.get("name") == "delete":
            self.deleted.append(query["value"])
            return jsonify({"status": True})
        nzo = query.get("nzo_ids")
        if mode == "queue":
            return jsonify({"queue": {"slots": []}})
        step = self.plan.get("step")
        if step and step[0] == "failed":
            return jsonify({"history": {"slots": [{"nzo_id": nzo, "status": "Failed", "fail_message": step[1]}]}})
        folder = server.DOWNLOADS / self.hits[nzo]
        self.plan["build"](folder)
        return jsonify({"history": {"slots": [{"nzo_id": nzo, "status": "Completed", "storage": self.plan.get("storage") or str(folder)}]}})


def filing_name(job):
    return server.filing.safe(f"{job['artist']} - {job['title']} [{job['id']}]")


def edition(tracks):
    return {"id": "e", "title": "Can't Stop", "date": "1969", "status": "Official", "label": "", "tracks": tracks}


def song(n):
    return {"disc": 1, "number": n, "title": titles[n - 1], "seconds": round(FLAC(SOURCE[n - 1]).info.length), "artist": "Test Band"}


def album_for(numbers):
    tracks = [song(n) for n in numbers]
    return {"id": ALBUM_ID, "title": "Can't Stop", "artist": "Test Band", "year": "1969", "kind": "Album", "cover": "",
            "tracks": len(tracks), "tracks_list": tracks, "seconds": sum(t["seconds"] for t in tracks), "editions": [edition(tracks)],
            "minutes": 1, "label": "", "released": "1969", "discs": 1}


def build(numbers, tags=False, extra=()):
    """A release folder, with file names only by default, as many Usenet posts have."""
    def make(folder):
        if folder.exists():
            return
        (folder / "CD").mkdir(parents=True)
        for n in numbers:
            path = folder / "CD" / f"{n:02d} - {titles[n - 1]}.flac"
            shutil.copyfile(SOURCE[n - 1], path)
            audio = FLAC(path)
            audio.delete()
            if tags:
                audio["TITLE"], audio["TRACKNUMBER"] = titles[n - 1], str(n)
            audio.save()
        (folder / "cover.jpg").write_bytes(b"x")
        (folder / "notes.nfo").write_text("x")
        for name, data in extra:
            (folder / "CD" / name).write_bytes(data)
    return make


def run_job(queue, fakes, album, build_with=None, partial=False):
    queue.brainz.album = lambda _: album
    if build_with:
        fakes.plan = {"build": build_with}
    for _ in range(600):
        time.sleep(0.05)
        if queue.jobs[-1]["state"] in ("done", "failed") and queue.running is None:
            return queue.jobs[-1]
    raise TimeoutError


def retry_through_the_page(queue, job, how):
    """The same code the page's Get the rest / Import anyway buttons reach."""
    page = Flask("check")
    page.extensions["encore"] = SimpleNamespace(queue=queue)
    with page.test_request_context():
        encore_web.retry(job["id"], partial=how == "partial", keep_format=how == "import")


def main():
    root = Path(tempfile.mkdtemp(prefix="encore-check-"))
    server.MUSIC, server.IMPORT, server.DOWNLOADS = root / "Music", root / "Import", root / "Encore"
    server.DATA, server.JOBS_FILE = root / "data", root / "data" / "jobs.json"
    server.POLL = 0.05
    for folder in (server.MUSIC, server.IMPORT, server.DOWNLOADS, server.DATA):
        folder.mkdir()
    fakes = Fakes()
    fake_app = Flask("fakes")
    fake_app.add_url_rule("/api", "indexer", fakes.indexer)
    fake_app.add_url_rule("/sab/api", "sab", fakes.sab, methods=["POST"])
    runner = make_server("127.0.0.1", 18999, fake_app, threaded=True)
    threading.Thread(target=runner.serve_forever, daemon=True).start()

    settings = sources.Settings(root / "data" / "settings.json")
    settings.data.update({"indexers": [{"name": "Fake", "url": "http://127.0.0.1:18999", "key": SECRET}],
                          "sab": {"url": "http://127.0.0.1:18999/sab", "key": SECRET}})
    settings.save()
    expect("settings are private", oct((root / "data" / "settings.json").stat().st_mode)[-3:] == "600")
    expect("page never gets keys", SECRET not in str(settings.public()), settings.public()["sab"])
    settings.update({"indexers": [{"name": "Fake", "url": "http://127.0.0.1:18999", "key": ""}],
                     "sab": {"url": "http://127.0.0.1:18999/sab", "key": ""}})
    expect("blank key keeps the saved one", settings.data["indexers"][0]["key"] == SECRET and settings.data["sab"]["key"] == SECRET)

    site = sources.Web()
    indexers = sources.Indexers(settings, site)
    sab = sources.Sabnzbd(settings, site)
    library = server.Library()
    library.build()
    brainz = SimpleNamespace(album=None)
    store = Store(root / "data" / "check.db")
    queue = server.Queue(indexers, sab, brainz, library, SimpleNamespace(linked=False), store)
    threading.Thread(target=queue.run, daemon=True).start()

    items, problems = indexers.search("Test Band", "Can’t Stop")
    expect("split apostrophe spelling is tried", [i["title"] for i in items] == ["Test Band - Can.t Stop (1969) FLAC"], problems)
    expect("search leaves the key out of results", SECRET not in str(items))
    offer = items[0]

    divide = {"title": "÷", "artist": "Ed Sheeran", "year": "2017", "seconds": 3400, "tracks_list": []}
    expect("a symbol album is searched by its name too", "Ed Sheeran divide" in sources.spellings("Ed Sheeran", "÷"))
    expect("a symbol album matches its spoken name", match.score(divide, {"title": "Ed_Sheeran-Divide-2017-FLAC", "size": 4 * 10**8})["tier"] != "no")
    expect("a discography isn't the album", match.score(divide, {"title": "Ed Sheeran - Albums Flac", "size": 11 * 10**9})["tier"] == "no")
    reports, copies = [], root / "copies"
    server.filing.Progress.EVERY = 0
    server.filing.copy_checked([(path, copies / path.name) for path in SOURCE], lambda text, percent: reports.append((text, percent)))
    texts = [text for text, _ in reports]
    expect("filing reports its progress", texts[0] == "Copying 1 of 4 files" and "Checking 4 of 4 copies" in texts
           and [p for _, p in reports] == sorted(p for _, p in reports) and reports[-1][1] <= 99, reports[-3:])
    server.filing.Progress.EVERY = 2

    for name in ("× (2014)", "Play (2025)"):
        (server.MUSIC / "Ed Sheeran" / name).mkdir(parents=True)
    shelf = server.Library()
    shelf.build()
    expect("symbol albums aren't each other", [bool(shelf.find("Ed Sheeran", t)) for t in ("×", "−", "÷", "=", "+")] == [True] + [False] * 4)
    expect("a symbol album still matches its own folder", shelf.find("Ed Sheeran", "×")["folder"] == "× (2014)")
    server.shutil.rmtree(server.MUSIC / "Ed Sheeran")

    def enqueue(album):
        queue.jobs.append(None)
        queue.jobs.pop()
        return queue.add(album, offer, True)

    album = album_for([1, 2, 3, 4])
    store.want(album)
    job = enqueue(album)
    finished = run_job(queue, fakes, album, build([1, 2, 3, 4]))
    expect("filing clears it from Wanted", not store.wanted_ids())
    expect("album with no tags files", finished["state"] == "done" and finished["where"] == "library", finished["message"])
    filed = sorted(p.name for p in (server.MUSIC / finished["folder"]).iterdir())
    expect("track names from MusicBrainz", "01 - Come Together.flac" in filed and "03 - Maxwell's Silver Hammer.flac" in filed, filed)
    expect("cover kept, nfo dropped", "cover.jpg" in filed and "notes.nfo" not in filed)
    audio = FLAC(server.MUSIC / finished["folder"] / "02 - Something.flac")
    expect("tags written", audio["ALBUM"] == ["Can't Stop"] and audio["ALBUMARTIST"] == ["Test Band"] and audio["DATE"] == ["1969"], finished["folder"])
    expect("own category made once", fakes.cats.count("encore") == 1)
    expect("download cleared after filing", not list(server.DOWNLOADS.iterdir()) and job["storage"] is None and fakes.deleted)
    expect("no key in saved jobs", SECRET not in server.JOBS_FILE.read_text())

    queue.jobs.clear()
    server.shutil.rmtree(server.MUSIC / "Test Band")
    job = enqueue(album)
    finished = run_job(queue, fakes, album, build([1, 2, 3]))
    expect("missing track is held", finished["state"] == "failed" and finished.get("can_partial") and finished.get("kept"), finished["message"])
    folder = Path(job["storage"])
    expect("kept for Get the rest", folder.exists())
    retry_through_the_page(queue, job, "partial")
    queue.wake.set()
    fakes.plan = {"build": lambda _: expect("no second download", False)}
    finished = run_job(queue, fakes, album)
    expect("Get the rest files it", finished["state"] == "done" and "Missing" in finished["message"], finished["message"])
    expect("three tracks filed", len(list((server.MUSIC / finished["folder"]).glob("*.flac"))) == 3)

    queue.jobs.clear()
    server.shutil.rmtree(server.MUSIC / "Test Band")
    other = album_for([1, 2, 3, 4])
    for number, track in enumerate(other["tracks_list"]):
        track["title"], track["seconds"] = f"Quite Different Name {number}", 999
    other["editions"] = [edition(other["tracks_list"])]
    enqueue(other)
    finished = run_job(queue, fakes, other, build([1, 2, 3, 4]))
    expect("wrong album refused", finished["state"] == "failed" and "isn't the album" in finished["message"], finished["message"])
    expect("wrong album not filed", not list(server.MUSIC.iterdir()) and not list(server.IMPORT.iterdir()))
    expect("wrong album removed", not list(server.DOWNLOADS.iterdir()))

    queue.jobs.clear()
    enqueue(album)
    finished = run_job(queue, fakes, album, build([1, 2, 3, 4], extra=[("setup.exe", b"MZ")]))
    expect("program refused", finished["state"] == "failed" and "setup.exe" in finished["message"], finished["message"])

    queue.jobs.clear()
    enqueue(album)
    finished = run_job(queue, fakes, album, build([1, 2, 3, 4], extra=[("05.mp3", b"x" * 500)]))
    expect("surprise format waits for a choice", finished["state"] == "failed" and finished.get("can_import") and finished.get("kept"), finished["message"])

    queue.jobs.clear()
    mp3s = root / "mp3s"
    mp3s.mkdir()
    for n in (1, 2, 3, 4):
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(SOURCE[n - 1]), "-map_metadata", "-1", "-b:a", "192k",
                        str(mp3s / f"{n:02d} - {titles[n - 1]}.mp3")], check=True)

    def build_mp3(folder):
        if not folder.exists():
            folder.mkdir()
            for path in mp3s.iterdir():
                shutil.copyfile(path, folder / path.name)

    lossy = album_for([1, 2, 3, 4])
    for track in lossy["tracks_list"]:
        track["seconds"] = round(mutagen.File(mp3s / f"{track['number']:02d} - {track['title']}.mp3").info.length)
    job = enqueue(lossy)
    finished = run_job(queue, fakes, lossy, build_mp3)
    expect("MP3 not chosen waits", finished["state"] == "failed" and finished.get("can_import"), finished["message"])
    folder = Path(job["storage"])
    retry_through_the_page(queue, job, "import")
    queue.wake.set()
    fakes.plan = {"build": lambda _: expect("no second download for import", False)}
    finished = run_job(queue, fakes, lossy)
    expect("Import anyway files it", finished["state"] == "done" and finished["where"] == "library", finished["message"])
    filed = sorted(p.name for p in (server.MUSIC / finished["folder"]).iterdir())
    expect("MP3 names and tags", "01 - Come Together.mp3" in filed and finished["quality"].startswith("MP3"), [filed[0], finished["quality"]])
    tags = mutagen.File(server.MUSIC / finished["folder"] / "01 - Come Together.mp3", easy=True)
    expect("MP3 tagged", tags["album"] == ["Can't Stop"] and tags["albumartist"] == ["Test Band"] and tags["tracknumber"] == ["1"])
    server.shutil.rmtree(server.MUSIC / "Test Band")

    queue.jobs.clear()
    queue.add(lossy, offer, True, True)
    finished = run_job(queue, fakes, lossy, build_mp3)
    expect("chosen MP3 files straight away", finished["state"] == "done" and finished["where"] == "library", finished["message"])
    server.shutil.rmtree(server.MUSIC / "Test Band")

    queue.jobs.clear()
    def build_junk(folder):
        folder.mkdir(exist_ok=True)
        (folder / "readme.txt").write_text("x")
    queue.add(lossy, offer, True, True)
    finished = run_job(queue, fakes, lossy, build_junk)
    expect("no music still refused", finished["state"] == "failed" and "No music" in finished["message"], finished["message"])

    queue.jobs.clear()
    short = album_for([1, 2, 3])
    enqueue(short)
    finished = run_job(queue, fakes, short, build([1, 2, 3, 4], tags=True))
    expect("unlisted bonus track goes to MusicImport", finished["state"] == "done" and finished["where"] == "import", finished["message"])
    expect("nothing in the library from it", not list(server.MUSIC.iterdir()))

    queue.jobs.clear()
    enqueue(album)
    outside = root / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    fakes.plan = {"build": lambda _: None, "storage": str(outside)}
    finished = run_job(queue, fakes, album)
    expect("folder outside Downloads refused", finished["state"] == "failed" and "can't use" in finished["message"], finished["message"])
    expect("folder outside Downloads untouched", (outside / "keep.txt").exists())

    queue.jobs.clear()
    enqueue(album)
    fakes.plan = {"step": ("failed", "Aborted, cannot be completed")}
    finished = run_job(queue, fakes, album)
    expect("SABnzbd failure reported", finished["state"] == "failed" and "Aborted" in finished["message"], finished["message"])

    queue.jobs.clear()
    offer = {**offer, "guid": "nope"}
    enqueue(album)
    finished = run_job(queue, fakes, album)
    expect("indexer refusal reported", finished["state"] == "failed" and "Limit reached" in finished["message"], finished["message"])
    expect("key never in a message", SECRET not in str(queue.jobs))

    queue.jobs.clear()
    offer = items[0]

    divide = {"title": "÷", "artist": "Ed Sheeran", "year": "2017", "seconds": 3400, "tracks_list": []}
    expect("a symbol album is searched by its name too", "Ed Sheeran divide" in sources.spellings("Ed Sheeran", "÷"))
    expect("a symbol album matches its spoken name", match.score(divide, {"title": "Ed_Sheeran-Divide-2017-FLAC", "size": 4 * 10**8})["tier"] != "no")
    expect("a discography isn't the album", match.score(divide, {"title": "Ed Sheeran - Albums Flac", "size": 11 * 10**9})["tier"] == "no")
    reports, copies = [], root / "copies"
    server.filing.Progress.EVERY = 0
    server.filing.copy_checked([(path, copies / path.name) for path in SOURCE], lambda text, percent: reports.append((text, percent)))
    texts = [text for text, _ in reports]
    expect("filing reports its progress", texts[0] == "Copying 1 of 4 files" and "Checking 4 of 4 copies" in texts
           and [p for _, p in reports] == sorted(p for _, p in reports) and reports[-1][1] <= 99, reports[-3:])
    server.filing.Progress.EVERY = 2

    for name in ("× (2014)", "Play (2025)"):
        (server.MUSIC / "Ed Sheeran" / name).mkdir(parents=True)
    shelf = server.Library()
    shelf.build()
    expect("symbol albums aren't each other", [bool(shelf.find("Ed Sheeran", t)) for t in ("×", "−", "÷", "=", "+")] == [True] + [False] * 4)
    expect("a symbol album still matches its own folder", shelf.find("Ed Sheeran", "×")["folder"] == "× (2014)")
    server.shutil.rmtree(server.MUSIC / "Ed Sheeran")
    clip = root / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc=duration=25:size=640x360:rate=10",
                    "-f", "lavfi", "-i", "sine=duration=25", "-shortest", "-pix_fmt", "yuv420p", str(clip)], check=True)
    (server.MUSIC / "Test Band").mkdir()

    def build_video(folder):
        if not folder.exists():
            folder.mkdir()
            shutil.copyfile(clip, folder / "x264-junk-name.mp4")
            (folder / "sample.nfo").write_text("x")

    artist = {"id": "a", "name": "Test Band"}
    verdict = {"song": "Great Song"}
    job = queue.add_video(artist, offer, verdict)
    finished = run_job(queue, fakes, album, build_video)
    expect("video filed for Plex", finished["state"] == "done" and (server.MUSIC / "Test Band" / "Great Song-video.mp4").exists(), [finished["message"], finished["quality"]])
    expect("video quality read", finished["quality"].startswith("360p"), finished["quality"])
    expect("video download cleared", job["storage"] is None)
    single = {}

    def build_single(folder):
        if not folder.exists():
            folder.mkdir()
            shutil.copyfile(clip, folder / "only.mkv")
        single["file"] = str(folder / "only.mkv")

    fakes.plan = {"build": build_single, "storage": ""}
    job = queue.add_video(artist, offer, {"song": "Lone File"})
    fakes.plan["storage"] = str(server.DOWNLOADS / filing_name(job) / "only.mkv")
    build_single(server.DOWNLOADS / filing_name(job))
    finished = run_job(queue, fakes, album)
    expect("SABnzbd naming the file itself works", finished["state"] == "done" and (server.MUSIC / "Test Band" / "Lone File-video.mkv").exists(), finished["message"])
    expect("single file's folder cleared", not Path(single["file"]).parent.exists())
    queue.add_video(artist, offer, verdict)
    finished = run_job(queue, fakes, album, build_video)
    expect("video never overwritten", finished["state"] == "failed" and "already have" in finished["message"], finished["message"])
    def build_audio_only(folder):
        if not folder.exists():
            folder.mkdir()
            (folder / "not-a-video.mp4").write_bytes(b"x" * 5000)
    queue.add_video(artist, offer, {"song": "Other"})
    finished = run_job(queue, fakes, album, build_audio_only)
    expect("fake video refused", finished["state"] == "failed" and "playable" in finished["message"], finished["message"])
    songs = {"Coldplay_Feat_Rihanna-Princess_Of_China-x264-2012-SRPx": "Princess Of China",
             "Coldplay-Adventure_Of_A_Lifetime-DDC-720p-x264-2015-LOVERS": "Adventure Of A Lifetime",
             'Just a dump: (with 15_percent pars) "Coldplay - The Scientist.mpg.vol21+18.PAR2"': "The Scientist",
             '90s tape - "Coldplay - To the end(lbu).avi"': "To the end",
             "Coldplay-Higher_Power_(Radio_1s_Big_Weekend_2021)-720p-x264-2021-SRPx": "Higher Power",
             "Coldplay-Christmas.Lights-DVDRiP-x264-2010-LOVERS": "Christmas Lights"}
    for title, song in songs.items():
        expect(f"song from {title[:40]}", match.video_song("Coldplay", title) == song, match.video_song("Coldplay", title))
    live = match.score_video("Coldplay", {"title": "Coldplay-Higher_Power_(Radio_1s_Big_Weekend_2021)-720p-x264-2021-SRPx", "size": 183 * 10**6})
    expect("live marked", live["tier"] == "maybe" and "Live performance" in live["notes"], live)

    runner.shutdown()
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(TONES, ignore_errors=True)


main()
sys.exit(0 if all(results) else 1)
