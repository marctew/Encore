"""Encore: find an album on MusicBrainz, pick a release from the Usenet indexers, let SABnzbd
fetch it, check it and file it.

An album only reaches the library once every track is present, is FLAC, decodes and fits a
MusicBrainz edition. Downloads land in their own SABnzbd category under DOWNLOADS, and nothing
outside that folder is ever deleted.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mutagen
from aiohttp import web
from mutagen.flac import FLAC

import filing
import match
from auth import MIN_LENGTH, SESSION_DAYS, Auth
from filing import Hold, JobFailed, norm
from plex import Plex
from sources import Deezer, Indexers, MusicBrainz, Sabnzbd, Settings, SourceError, Web
from store import Store

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS_FILE = DATA / "jobs.json"
PHOTOS = DATA / "photos"
NOWHERE = Path("/nonexistent/encore")
MUSIC = IMPORT = DOWNLOADS = NOWHERE
STATIC = Path(__file__).parent / "static"
PORT = int(os.environ.get("ENCORE_PORT", 3346))
MIN_FREE = 5 * 1024**3
INDEX_AGE = 300
POLL = 3
PLEX_SYNC = 3600
PHOTO_RETRY = 30 * 86400
ARTIST_LIMIT = 12
COOKIE = "encore_session"
OPEN_PATHS = ("/login", "/api/login")
LOST_AFTER = 40
ACTIVE = ("queued", "downloading", "verifying", "filing", "scanning")
ALBUM_ID = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
AUDIO = (".flac", ".mp3", ".m4a", ".wav", ".ape", ".wv", ".aiff", ".aif", ".ogg", ".opus", ".wma", ".dsf", ".dff")
PROGRAMS = (".exe", ".scr", ".bat", ".cmd", ".com", ".msi", ".ps1", ".vbs", ".js", ".jar", ".lnk", ".dll")
COMPILATION = {"various artists", "various", "va"}
VIDEOS = (".mp4", ".mkv", ".avi", ".mpg", ".mpeg", ".vob", ".ts", ".m4v", ".mov", ".wmv")
VIDEO_SECONDS = 20

log = logging.getLogger("encore")
pool = ThreadPoolExecutor(4)


class Cancelled(Exception):
    pass


class Incomplete(JobFailed):
    """Tracks are missing, so the person can choose to take the rest."""


class NotFlac(JobFailed):
    """The release said FLAC and wasn't, so the person can choose to keep it anyway."""


def use_folders(settings):
    """Folders come from Settings, so a change takes effect without a restart."""
    global MUSIC, IMPORT, DOWNLOADS
    folders = settings.data["folders"]
    MUSIC = Path(folders["music"]) if folders["music"] else MUSIC
    IMPORT = Path(folders["imports"]) if folders["imports"] else IMPORT
    DOWNLOADS = Path(folders["downloads"]) if folders["downloads"] else DOWNLOADS


def folders_ready():
    return MUSIC.is_dir() and IMPORT.is_dir() and DOWNLOADS != NOWHERE


def can_write(folder):
    try:
        with tempfile.NamedTemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def album_key(name):
    name = re.sub(r"^\(\d{4}\)\s*", "", name.strip())
    previous = None
    while name != previous:
        previous = name
        name = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", name)
        name = re.sub(r"\s+320$", "", name).strip()
    return norm(name)


class Library:
    """Album folders under the music share, matched to results by name."""

    def __init__(self):
        self.artists = {}
        self.built = 0
        self.qualities = {}

    def build(self):
        artists = {}
        for artist in os.scandir(MUSIC) if MUSIC.is_dir() else ():
            if not artist.is_dir() or artist.name.startswith("."):
                continue
            albums = artists.setdefault(norm(artist.name), [])
            for album in os.scandir(artist.path):
                if album.is_dir() and not album.name.startswith("."):
                    albums.append((album_key(album.name), album.path, album.name))
        self.artists, self.built = artists, time.time()

    def stale(self):
        return time.time() - self.built > INDEX_AGE

    def quality(self, path):
        if path not in self.qualities:
            self.qualities[path] = self.read_quality(path)
        return self.qualities[path]

    @staticmethod
    def read_quality(path):
        for folder, _, files in os.walk(path):
            for name in sorted(files):
                if not name.lower().endswith((".flac", ".mp3", ".m4a")):
                    continue
                try:
                    info = mutagen.File(os.path.join(folder, name)).info
                except Exception:
                    return None
                bits = getattr(info, "bits_per_sample", 0)
                if bits:
                    return f"{bits}/{info.sample_rate / 1000:g}"
                return f"{Path(name).suffix[1:].upper()} {getattr(info, 'bitrate', 0) // 1000}"
        return None

    def find(self, artist, title):
        wanted = album_key(title)
        close = None
        for key, path, name in self.artists.get(norm(artist), []):
            if key == wanted:
                return {"match": "exact", "folder": name, "quality": self.quality(path)}
            if min(len(key), len(wanted)) >= 6 and (key.startswith(wanted) or wanted.startswith(key)):
                close = close or (path, name)
        if close:
            return {"match": "close", "folder": close[1], "quality": self.quality(close[0])}
        return None


def inside_downloads(path):
    """SABnzbd reports the folder, so it's checked before anything is changed or deleted."""
    try:
        return DOWNLOADS.resolve() in Path(path).resolve().parents
    except OSError:
        return False


class Queue:
    def __init__(self, indexers, sab, brainz, library, plex, store, on_filed=None):
        self.store = store
        self.on_filed = on_filed
        self.indexers = indexers
        self.sab = sab
        self.brainz = brainz
        self.library = library
        self.plex = plex
        self.jobs = []
        self.offers = {}
        self.wake = asyncio.Event()
        self.running = None
        self.load()

    def load(self):
        if JOBS_FILE.exists():
            self.jobs = json.loads(JOBS_FILE.read_text())
        for folder in [*MUSIC.glob(".encore-incoming-*"), *IMPORT.glob("_encore-incoming-*")]:
            shutil.rmtree(folder, ignore_errors=True)
        for job in self.jobs:
            if job["state"] in ("downloading", "verifying", "filing"):
                job.update(state="queued", percent=0, message="")
        self.save()

    async def forget(self, job):
        """Removes one job's download from SABnzbd and the share. The worst this costs is a fresh download."""
        job["kept"] = job["reuse"] = False
        nzo, job["nzo"] = job.get("nzo"), None
        storage, job["storage"] = job.get("storage"), None
        if nzo:
            await self.sab.forget(nzo)
        if storage and inside_downloads(storage):
            await asyncio.get_running_loop().run_in_executor(pool, shutil.rmtree, storage, True)

    def save(self):
        temp = JOBS_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(self.jobs, indent=1))
        temp.replace(JOBS_FILE)

    def get(self, job_id):
        return next((j for j in self.jobs if j["id"] == job_id), None)

    def add(self, album, offer, fix_artist, any_format=False):
        job = {key: album[key] for key in ("title", "artist", "year", "tracks", "cover")}
        job.update(album_id=album["id"], id=uuid.uuid4().hex[:12], state="queued", percent=0, message="",
                   fix_artist=fix_artist, folder="", quality="", added=time.time(), partial=False, any_format=any_format,
                   source={k: offer[k] for k in ("indexer", "guid", "title", "size", "key")})
        self.jobs.append(job)
        self.update()
        self.wake.set()
        return job

    def add_video(self, artist, offer, verdict):
        job = {"title": verdict["song"], "artist": artist["name"], "artist_id": artist["id"], "year": "", "tracks": 1,
               "cover": f"/api/artist/{artist['id']}/photo", "album_id": f"video-{offer['key']}", "kind": "video",
               "id": uuid.uuid4().hex[:12], "state": "queued", "percent": 0, "message": "", "fix_artist": False,
               "folder": "", "quality": "", "added": time.time(), "partial": False,
               "source": {k: offer[k] for k in ("indexer", "guid", "title", "size", "key")}}
        self.jobs.append(job)
        self.update()
        self.wake.set()
        return job

    def update(self, job=None, **changes):
        if job:
            job.update(changes)
        self.save()

    async def run(self):
        for job in self.jobs:
            if job["state"] == "scanning":
                asyncio.create_task(self.announce(job))
        while True:
            job = next((j for j in self.jobs if j["state"] == "queued"), None)
            if not job:
                self.wake.clear()
                await self.wake.wait()
                continue
            self.running = job
            try:
                await self.process_job(job)
                self.finish(job)
            except Cancelled:
                self.update(job, state="failed", message="Cancelled.")
            except SourceError as error:
                self.update(job, state="failed", message=f"{error}.")
            except Incomplete as error:
                self.update(job, state="failed", message=str(error), can_partial=True, kept=True)
            except NotFlac as error:
                self.update(job, state="failed", message=str(error), can_import=True, kept=True)
            except JobFailed as error:
                self.update(job, state="failed", message=str(error))
            except Exception:
                log.exception("Job failed")
                self.update(job, state="failed", message="Something went wrong. See the Encore log.")
            finally:
                if job.get("state") != "scanning" and not job.get("kept"):
                    await self.forget(job)
                    self.update()
                self.running = None

    def result(self, job, message):
        notes = [job.get("partial_note"), job.get("edition_note")]
        return " ".join([message, *filter(None, notes)])

    def finish(self, job):
        self.store.unwant(job["album_id"])
        if job["where"] == "import":
            self.update(job, state="done", message=self.result(job, job["reason"]), finished=time.time())
        elif self.plex.linked:
            self.update(job, state="scanning", message="Plex is scanning.")
            asyncio.create_task(self.announce(job))
        else:
            self.update(job, state="done", finished=time.time(),
                        message=self.result(job, "In your library. Connect Plex so it scans by itself."))

    async def video_in_plex(self, job):
        await self.plex.scan(job["folder"].split("/")[0])
        row = self.store.plex_artist(job["artist_id"])
        if not row:
            return False
        return await self.plex.video_shown(row["rating"], job["folder"].split("/")[-1])

    async def announce(self, job):
        try:
            if job.get("kind") == "video":
                shown = await self.video_in_plex(job)
            else:
                await self.plex.scan(job["folder"])
                shown = await self.plex.confirm(job["artist"], job["folder"], job["tracks"])
            message = "In your library and Plex." if shown else "In your library, but Plex hasn't shown it yet."
        except Exception as error:
            log.warning("Plex scan failed: %s", type(error).__name__)
            message = "In your library, but the Plex scan failed. Run Scan Library Files."
        await self.forget(job)
        self.update(job, state="done", message=self.result(job, message), finished=time.time())
        if self.on_filed:
            await self.on_filed()

    def check_cancel(self, job):
        if job.get("cancel"):
            job.pop("cancel")
            raise Cancelled

    async def process_job(self, job):
        loop = asyncio.get_running_loop()
        job["kept"] = False
        for key in ("partial_note", "edition_note", "can_partial", "can_import"):
            job.pop(key, None)
        self.update(job, state="downloading", message="")
        if job.get("kind") == "video":
            source = await self.download(job)
            self.check_cancel(job)
            self.update(job, state="verifying", message="Checking the video.")
            chosen = await loop.run_in_executor(pool, prepare_video, source, job)
            self.update(job, state="filing", message="Filing the video.")
            job.update(where="library", folder=await loop.run_in_executor(
                pool, filing.to_artist, chosen, job["artist"], job["title"], MUSIC, job["id"]))
            return
        album = await self.brainz.album(job["album_id"])
        if job.pop("reuse", False) and job.get("storage"):
            source = Path(job["storage"])
        else:
            source = await self.download(job)
        self.check_cancel(job)
        self.update(job, state="verifying", message="Checking every track.")
        hold = await loop.run_in_executor(pool, prepare, source, album, job)
        self.check_cancel(job)
        self.update(job, state="filing", message="Filing the album.")
        job.update(await loop.run_in_executor(pool, file_album, source, job["id"], self.library, hold))

    async def download(self, job):
        size = job["source"]["size"]
        await asyncio.get_running_loop().run_in_executor(pool, make_folder, DOWNLOADS)
        if shutil.disk_usage(DOWNLOADS).free < size * 2 + MIN_FREE:
            raise JobFailed("Not enough free space on the share for this album.")
        if not job.get("nzo"):
            nzb = await self.indexers.fetch(job["source"]["indexer"], job["source"]["guid"])
            name = filing.safe(f"{job['artist']} - {job['title']} [{job['id']}]")
            self.update(job, nzo=await self.sab.add(nzb, name), percent=0)
        return await self.wait(job)

    async def wait(self, job):
        silent = 0
        while True:
            self.check_cancel(job)
            try:
                state = await self.sab.state(job["nzo"])
                silent = 0 if state else silent + 1
            except SourceError:
                state, silent = None, silent + 1
            if silent > LOST_AFTER:
                raise JobFailed("SABnzbd lost track of this download.")
            if state:
                kind, value = state
                if kind == "queue" and value != job.get("percent"):
                    self.update(job, percent=value, step="")
                elif kind == "working" and value != job.get("step"):
                    self.update(job, percent=100, step=value)
                elif kind == "failed":
                    raise JobFailed(f"SABnzbd couldn't finish it: {value}. Try another release.")
                elif kind == "done":
                    # SABnzbd names the file itself when a download unpacks to a single file.
                    folder = os.path.dirname(value) if os.path.isfile(value) else value
                    if not inside_downloads(folder) or not os.path.isdir(folder):
                        raise JobFailed("SABnzbd finished it somewhere Encore can't use.")
                    self.update(job, storage=folder)
                    return Path(folder)
            await asyncio.sleep(POLL)


def make_folder(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o777)


def count(tracks):
    return f"{tracks} track{'' if tracks == 1 else 's'}"


def and_more(titles):
    more = f" and {len(titles) - 1} more" if len(titles) > 1 else ""
    return f"“{titles[0]}”{more}"


def audio_files(folder):
    return sorted(f for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO and f.stat().st_size > 0)


def describe_file(path):
    """Uses the tags where there are any, otherwise whatever the file name offers."""
    audio = mutagen.File(path, easy=True)
    first = lambda key: (audio.get(key) or [""])[0]
    name = re.match(r"^\s*(?:(\d)[-. ]+)?(\d{1,3})\s*[-. _]*(.*)$", path.stem)
    return {"path": path, "seconds": audio.info.length, "title": first("title") or (name.group(3) if name else path.stem),
            "track": filing.number(first("tracknumber")) or (int(name.group(2)) if name else None),
            "disc": filing.number(first("discnumber")) or 1}


def decode_ok(path):
    """Decodes every sample. Some FLACs have no checksum, so counting samples against
    the header is what catches a cut-off track on those."""
    if path.suffix.lower() != ".flac":
        return subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    try:
        info = FLAC(path).info
    except Exception:
        return False
    process = subprocess.Popen(["flac", "--decode", "--stdout", "--silent", "--force-raw-format",
                                "--endian=little", "--sign=signed", str(path)],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = 0
    while block := process.stdout.read(1 << 20):
        size += len(block)
    expected = info.total_samples * info.channels * ((info.bits_per_sample + 7) // 8)
    return process.wait() == 0 and expected > 0 and size == expected


def pool_map(action, items):
    with ThreadPoolExecutor(2) as workers:
        return list(workers.map(action, items))


def write_tags(pairs, album, edition, fix_artist):
    """Filing and Plex both run on tags, and Usenet tags can be anything, so MusicBrainz's go on."""
    multi = len({track["disc"] for track in edition["tracks"]}) > 1
    keep_artist = norm(album["artist"]) in COMPILATION or not fix_artist
    for item, track in pairs:
        audio = mutagen.File(item["path"], easy=True)
        if audio.tags is None:
            audio.add_tags()
        audio["album"], audio["albumartist"], audio["title"] = album["title"], album["artist"], track["title"]
        audio["artist"] = (track["artist"] or album["artist"]) if keep_artist else album["artist"]
        audio["tracknumber"], audio["discnumber"] = str(track["number"]), str(track["disc"] if multi else 1)
        if album["year"]:
            audio["date"] = album["year"]
        audio.save()


def prepare(folder, album, job):
    """Returns a reason to send the album to MusicImport instead of the library, or None."""
    every = [f for f in folder.rglob("*") if f.is_file()]
    programs = [f.name for f in every if f.suffix.lower() in PROGRAMS]
    if programs:
        raise JobFailed(f"It has a program in it ({programs[0]}), so nothing was filed. Try another release.")
    files = audio_files(folder)
    if not files:
        raise JobFailed("No music arrived. It may be passworded or the wrong kind of post.")
    wrong = [f for f in files if f.suffix.lower() != ".flac"]
    if wrong and not job.get("any_format"):
        raise NotFlac(f"Not all FLAC ({count(len(wrong))} in another format), so it's waiting for your choice.")
    if any(f.suffix.lower() not in filing.TAGGED for f in files):
        job.update(tracks=len(files), quality=files[0].suffix[1:].upper())
        return "Encore can't tag this format, so it's here for you to file."
    tracks = [describe_file(f) for f in files]
    if len(tracks) == 1 and album["tracks"] > 2:
        if job.get("any_format"):
            job.update(tracks=1, quality=files[0].suffix[1:].upper())
            return "It's one long file rather than separate tracks, so it's here for you to file."
        raise JobFailed("It's one long file rather than separate tracks. Try another release.")
    fit = match.best_edition(tracks, album["editions"])
    edition = fit["edition"]
    wanted = len(edition["tracks"])
    if len(fit["pairs"]) < max(1, wanted * 0.6):
        raise JobFailed(f"This isn't the album: only {len(fit['pairs'])} of {wanted} tracks match. Try another release.")
    gone = [t["title"] for t in fit["missing"]]
    hold = None
    if fit["extra"] and not gone:
        hold = f"It has {count(len(fit['extra']))} no MusicBrainz edition lists, so it's here for you to file."
    elif fit["extra"]:
        raise JobFailed(f"The tracks don't fit any edition of this album ({and_more(gone)} missing, {len(fit['extra'])} extra).")
    elif gone and not job.get("partial"):
        raise Incomplete(f"{and_more(gone)} didn't arrive, so {len(fit['pairs'])} of {wanted} tracks are here.")
    elif gone:
        job["partial_note"] = f"Missing {and_more(gone)}."
    bad = [f.name for f, ok in zip(files, pool_map(decode_ok, files)) if not ok]
    if bad:
        raise JobFailed(f"{count(len(bad))} failed the audio check, starting with {bad[0]}.")
    if not gone:
        seconds = sum(item["seconds"] for item, _ in fit["pairs"])
        listed = sum(track["seconds"] for _, track in fit["pairs"])
        if listed and abs(seconds - listed) > max(30, listed * 0.05):
            raise JobFailed(f"Audio runs {seconds / 60:.1f} min but the album is {listed / 60:.1f} min. Try another release.")
    if not hold:
        write_tags(fit["pairs"], album, edition, job["fix_artist"])
    if len(edition["tracks"]) != album["tracks"]:
        job["edition_note"] = f"This is the {len(edition['tracks'])}-track edition."
    job["quality"] = describe_quality(files)
    job["tracks"] = len(fit["pairs"]) if not hold else len(files)
    return hold


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                            capture_output=True, text=True)
    return json.loads(result.stdout or "{}") if result.returncode == 0 else {}


def prepare_video(folder, job):
    """Picks the main video in a download. There's no track list to check against, so
    it has to hold real video of a sensible length and read through to the end."""
    every = [f for f in folder.rglob("*") if f.is_file()]
    programs = [f.name for f in every if f.suffix.lower() in PROGRAMS]
    if programs:
        raise JobFailed(f"It has a program in it ({programs[0]}), so nothing was filed. Pick another.")
    videos = [f for f in every if f.suffix.lower() in VIDEOS]
    if not videos:
        raise JobFailed("No video arrived. It may be passworded or the wrong kind of post.")
    main = max(videos, key=lambda f: f.stat().st_size)
    info = probe(main)
    stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    seconds = float((info.get("format") or {}).get("duration") or 0)
    if not stream or seconds < VIDEO_SECONDS:
        raise JobFailed("That file isn't a playable video. Pick another.")
    check = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(main), "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode != 0:
        raise JobFailed("The video file is damaged. Pick another.")
    job["quality"] = f"{stream.get('height') or '?'}p · {int(seconds) // 60}:{int(seconds) % 60:02d}"
    return main


def describe_quality(files):
    infos = [mutagen.File(f).info for f in files]
    if all(f.suffix.lower() == ".flac" for f in files):
        best = max(infos, key=lambda i: (i.bits_per_sample, i.sample_rate))
        return f"{best.bits_per_sample}/{best.sample_rate / 1000:g}"
    return f"{files[0].suffix[1:].upper()} {max(getattr(i, 'bitrate', 0) for i in infos) // 1000}"


def file_album(source, job_id, library, hold=None):
    library.build()
    try:
        if hold:
            raise Hold(hold)
        target, names = filing.plan(source, MUSIC, library)
    except Hold as held:
        return {"where": "import", "reason": str(held), "folder": filing.to_import(source, IMPORT, job_id)}
    filing.to_library(source, target, names, MUSIC, job_id)
    return {"where": "library", "album": filing.read_track(filing.audio_in(target)[0])["album"],
            "folder": target.relative_to(MUSIC).as_posix()}


class Owned:
    """What Plex says you have, indexed for name matching. Plex's album ids are for single
    releases rather than MusicBrainz's album groups, so albums match on artist and title."""

    def __init__(self):
        self.by_artist = {}
        self.artists = set()

    def load(self, store):
        by_artist, artists = {}, set()
        for row in store.plex_albums():
            for name in filter(None, (row["mbid"], norm(row["artist"]))):
                by_artist.setdefault(name, {})[album_key(row["title"])] = row
        for row in store.plex_artists():
            artists.add(row["mbid"])
        self.by_artist, self.artists = by_artist, artists

    def find(self, album):
        held = self.by_artist.get(album.get("artist_id")) or self.by_artist.get(norm(album["artist"])) or {}
        return held.get(album_key(album["title"]))


settings_key = web.AppKey("settings", Settings)
auth_key = web.AppKey("auth", Auth)
store_key = web.AppKey("store", Store)
owned_key = web.AppKey("owned", Owned)
deezer_key = web.AppKey("deezer", Deezer)
filling_key = web.AppKey("filling", deque)
web_key = web.AppKey("web", Web)
brainz_key = web.AppKey("brainz", MusicBrainz)
indexers_key = web.AppKey("indexers", Indexers)
sab_key = web.AppKey("sab", Sabnzbd)
plex_key = web.AppKey("plex", Plex)
library_key = web.AppKey("library", Library)
queue_key = web.AppKey("queue", Queue)


def public(job):
    return {k: v for k, v in job.items() if k not in ("cancel", "fix_artist", "reuse", "nzo", "storage")}


async def fresh_library(app):
    library = app[library_key]
    if library.stale():
        await asyncio.get_running_loop().run_in_executor(pool, library.build)
    return library


async def mark_owned(app, albums):
    library = await fresh_library(app)

    owned, wanted = app[owned_key], app[store_key].wanted_ids()

    def find():
        for album in albums:
            album["owned"] = library.find(album["artist"], album["title"])
            held = None if album["owned"] else owned.find(album)
            if held:
                album["owned"] = {"match": "exact", "folder": f"{held['title']} in Plex", "quality": None}
            album["wanted"] = album["id"] in wanted

    await asyncio.get_running_loop().run_in_executor(pool, find)


async def search(request):
    query = request.query.get("q", "").strip()
    if not query:
        return web.json_response({"albums": []})
    store = request.app[store_key]
    ids, saved_at = store.search("album", norm(query))
    albums = [store.album(i) for i in ids] if ids is not None and "refresh" not in request.query else None
    if albums is None or None in albums:
        albums = await request.app[brainz_key].search(query)
        for album in albums:
            store.save_album(album)
        store.save_search("album", norm(query), [a["id"] for a in albums])
        saved_at = time.time()
    await mark_owned(request.app, albums)
    return web.json_response({"albums": albums, "saved_at": saved_at})


async def album_page(request):
    album_id = request.match_info["album"]
    if not ALBUM_ID.match(album_id):
        raise web.HTTPBadRequest(text="Bad album id")
    details = dict(await request.app[brainz_key].album(album_id, refresh="refresh" in request.query))
    details["editions"] = len(details["editions"])
    await mark_owned(request.app, [details])
    return web.json_response(details)


def offer_row(item, verdict, failed):
    return {"key": item["key"], "title": item["title"], "indexer": item["indexer"], "size": item["size"],
            "posted": item["posted"], "failed": failed, **verdict}


async def album_downloads(request):
    album_id = request.match_info["album"]
    if not ALBUM_ID.match(album_id):
        raise web.HTTPBadRequest(text="Bad album id")
    app = request.app
    queue = app[queue_key]
    if not app[indexers_key].active():
        raise SourceError("Add your indexer API keys in Settings first")
    album = await app[brainz_key].album(album_id)
    items, problems = await app[indexers_key].search(album["artist"], album["title"])
    failed = {j["source"]["key"]: j["message"] for j in queue.jobs if j["state"] == "failed" and j.get("source")}
    rows = []
    for item in items:
        queue.offers[item["key"]] = item
        rows.append(offer_row(item, match.score(album, item), failed.get(item["key"])))
    order = {"good": 0, "maybe": 1, "no": 2}
    rows.sort(key=lambda r: (bool(r["failed"]), order[r["tier"]], -r["score"], -r["size"]))
    return web.json_response({"results": rows, "problems": problems})


def has_words(name, words):
    return f" {words} " in f" {name} "


async def search_artists(request):
    words = norm(request.query.get("q", ""))
    if not words:
        return web.json_response({"artists": []})
    app = request.app
    store = app[store_key]
    ids, saved_at = store.search("artist", words)
    found = [store.artist(i) for i in ids] if ids is not None and "refresh" not in request.query else None
    if found is None or None in found:
        found = [a for a in await app[brainz_key].artists(request.query["q"]) if has_words(norm(a["name"]), words)]
        for artist in found:
            store.save_artist(artist)
        store.save_search("artist", words, [a["id"] for a in found])
        saved_at = time.time()
    held = app[owned_key].artists
    order = sorted(enumerate(found), key=lambda pair: (pair[1]["id"] not in held, norm(pair[1]["name"]) != words, pair[0]))
    named, artists = set(), []
    for _, artist in order[:ARTIST_LIMIT]:
        key = norm(artist["name"])
        artists.append(artist_card(artist, held, key not in named))
        named.add(key)
    return web.json_response({"artists": artists, "saved_at": saved_at})


def artist_card(artist, held, first_of_name=True):
    """Deezer matches on name alone, so only the likeliest artist of a name may use its photo."""
    fields = ("id", "name", "kind", "country", "begin", "end", "ended", "note", "tags")
    return {**{k: artist.get(k) for k in fields}, "in_library": artist["id"] in held, "best": first_of_name}


async def artist_page(request):
    artist_id = request.match_info["artist"]
    if not ALBUM_ID.match(artist_id):
        raise web.HTTPBadRequest(text="Bad artist id")
    app = request.app
    store, brainz = app[store_key], app[brainz_key]
    refresh = "refresh" in request.query
    artist = None if refresh else store.artist(artist_id)
    if not artist:
        store.save_artist(await brainz.artist(artist_id))
        if refresh:
            store.clear_photo(artist_id)
            (PHOTOS / f"{artist_id}.jpg").unlink(missing_ok=True)
        artist = store.artist(artist_id)
    if refresh or not artist["albums_at"]:
        store.save_albums(artist_id, await brainz.artist_albums(artist_id))
        artist = store.artist(artist_id)
    albums = store.albums_of(artist_id)
    albums.sort(key=lambda a: (a["year"], a["title"].casefold()), reverse=True)
    await mark_owned(app, albums)
    listed = {album_key(a["title"]) for a in albums}
    others = []
    for row in store.plex_albums():
        if row["mbid"] == artist_id and album_key(row["title"]) not in listed:
            others.append({"title": row["title"], "year": row["year"], "cover": f"/api/plex/thumb/{row['rating']}"})
    app[filling_key].extend(a["id"] for a in sorted(albums, key=lambda a: a["kind"] != "Album")
                            if not store.has_details(a["id"]) and a["id"] not in app[filling_key])
    request.app["fill"].set()
    return web.json_response({"artist": artist_card(artist, app[owned_key].artists), "albums": albums,
                              "others": others, "saved_at": artist["albums_at"]})


async def artist_photo(request):
    """Plex's photo where it has one, otherwise one fetched once and kept on disk."""
    artist_id = request.match_info["artist"]
    if not ALBUM_ID.match(artist_id):
        raise web.HTTPBadRequest(text="Bad artist id")
    app = request.app
    store = app[store_key]
    plex_row = store.plex_artist(artist_id)
    if plex_row and plex_row["thumb"]:
        return await plex_image(app, plex_row["thumb"])
    path = PHOTOS / f"{artist_id}.jpg"
    if path.exists():
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})
    artist = store.artist(artist_id)
    fresh = artist and artist["photo"] == "none" and time.time() - artist["photo_at"] < PHOTO_RETRY
    if not artist or fresh or request.query.get("best") == "0":
        raise web.HTTPNotFound()
    image = await app[deezer_key].photo(artist["name"])
    store.set_photo(artist_id, "local" if image else "none")
    if not image:
        raise web.HTTPNotFound()
    path.write_bytes(image)
    return web.Response(body=image, content_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


async def plex_image(app, thumb):
    try:
        body, kind = await app[plex_key].image(thumb)
    except Exception as error:
        log.warning("Plex image failed: %s", type(error).__name__)
        raise web.HTTPNotFound() from None
    return web.Response(body=body, content_type=kind, headers={"Cache-Control": "max-age=86400"})


async def plex_thumb(request):
    thumb = request.app[store_key].plex_thumb(request.match_info["rating"])
    if not thumb:
        raise web.HTTPNotFound()
    return await plex_image(request.app, thumb)


async def sync_plex(app):
    """Refreshes what Plex holds, and anything wanted that has since arrived stops being wanted."""
    artists, albums = await app[plex_key].library()
    store = app[store_key]
    store.replace_plex(artists, albums)
    app[owned_key].load(store)
    library = await fresh_library(app)
    for album in store.wanted():
        found = library.find(album["artist"], album["title"])
        if app[owned_key].find(album) or (found and found["match"] == "exact"):
            store.unwant(album["id"])


async def plex_rhythm(app):
    while True:
        if app[plex_key].linked and app[plex_key].server:
            try:
                await sync_plex(app)
            except Exception as error:
                log.warning("Plex sync failed: %s", type(error).__name__)
        await asyncio.sleep(PLEX_SYNC)


async def fill_albums(app):
    """Fetches track lists ahead of time, one at a time, stepping aside whenever the page needs MusicBrainz."""
    waiting, brainz = app[filling_key], app[brainz_key]
    while True:
        if not waiting:
            app["fill"].clear()
            await app["fill"].wait()
        album_id = waiting.popleft()
        while brainz.busy:
            await asyncio.sleep(0.5)
        try:
            await brainz.album(album_id, background=True)
        except SourceError:
            await asyncio.sleep(30)


async def artist_videos(request):
    artist_id = request.match_info["artist"]
    if not ALBUM_ID.match(artist_id):
        raise web.HTTPBadRequest(text="Bad artist id")
    app = request.app
    artist = app[store_key].artist(artist_id)
    if not artist:
        raise web.HTTPNotFound()
    if not app[indexers_key].active():
        raise SourceError("Add your indexer API keys in Settings first")
    items, problems = await app[indexers_key].videos(artist["name"])
    queue = app[queue_key]
    failed = {j["source"]["key"]: j["message"] for j in queue.jobs if j["state"] == "failed" and j.get("source")}
    rows = []
    for item in items:
        queue.offers[item["key"]] = item
        rows.append(offer_row(item, match.score_video(artist["name"], item), failed.get(item["key"])))
    order = {"good": 0, "maybe": 1, "no": 2}
    rows.sort(key=lambda r: (bool(r["failed"]), order[r["tier"]], -r["score"], r["song"].casefold()))
    return web.json_response({"results": rows, "problems": problems})


async def add_video(request):
    body = await request.json()
    app = request.app
    queue = app[queue_key]
    artist = app[store_key].artist(str(body.get("artist_id", "")))
    offer = queue.offers.get(str(body.get("release", "")))
    if not artist or not offer:
        raise web.HTTPConflict(text="That result has expired. Search for videos again.")
    if not app[sab_key].ready:
        raise web.HTTPConflict(text="Add your SABnzbd details in Settings first")
    if not folders_ready():
        raise web.HTTPConflict(text="Set your folders in Settings first")
    if any(j["album_id"] == f"video-{offer['key']}" and j["state"] in ACTIVE for j in queue.jobs):
        raise web.HTTPConflict(text="Already queued")
    verdict = match.score_video(artist["name"], offer)
    song = str(body.get("song") or verdict["song"]).strip()
    if not song:
        raise web.HTTPConflict(text="Encore can't tell which song this is")
    return web.json_response(public(queue.add_video(artist, offer, {**verdict, "song": song})))


async def library_page(request):
    store = request.app[store_key]
    artists = [{"id": a["mbid"], "name": a["name"], "albums": a["albums"], "rating": a["rating"]} for a in store.plex_artists()]
    return web.json_response({"artists": artists, "albums": len(store.plex_albums()), "saved_at": store.plex_at()})


async def library_sync(request):
    try:
        await sync_plex(request.app)
    except Exception as error:
        log.warning("Plex sync failed: %s", type(error).__name__)
        raise SourceError("Plex isn't answering") from None
    return await library_page(request)


async def wanted_list(request):
    return web.json_response({"albums": request.app[store_key].wanted()})


async def add_wanted(request):
    body = await request.json()
    store = request.app[store_key]
    added = 0
    for album_id in body.get("album_ids") or []:
        album = store.album(str(album_id))
        if album:
            store.want(album)
            added += 1
    return web.json_response({"added": added})


async def remove_wanted(request):
    request.app[store_key].unwant(request.match_info["album"])
    return web.json_response({"ok": True})


async def status(request):
    app = request.app
    sab = app[sab_key]
    state = "unset"
    if sab.ready:
        try:
            await sab.version()
            state = "ok"
        except SourceError:
            state = "error"
    library = await fresh_library(app)
    return web.json_response({"sab": state, "indexers": len(app[indexers_key].active()),
                              "wanted": len(app[store_key].wanted_ids()),
                              "plex": await app[plex_key].status(), "folders": folders_ready(),
                              "free": shutil.disk_usage(IMPORT if IMPORT.is_dir() else ROOT).free,
                              "albums": sum(len(a) for a in library.artists.values())})


async def get_settings(request):
    return web.json_response({**request.app[settings_key].public(), "password": request.app[auth_key].enabled})


def remember(response, request):
    response.set_cookie(COOKIE, request.app[auth_key].token(), max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax")


async def check_password(auth, password):
    return await asyncio.get_running_loop().run_in_executor(pool, auth.check, password)


async def login_page(request):
    auth = request.app[auth_key]
    if not auth.enabled or auth.valid(request.cookies.get(COOKIE)):
        raise web.HTTPFound("/")
    return web.FileResponse(STATIC / "login.html")


async def log_in(request):
    auth = request.app[auth_key]
    address = request.remote
    if auth.locked(address):
        return web.json_response({"error": "Too many tries. Wait a minute and try again."}, status=429)
    body = await request.json()
    if not await check_password(auth, str(body.get("password", ""))):
        auth.failed(address)
        return web.json_response({"error": "That isn't the password."}, status=401)
    auth.passed(address)
    response = web.json_response({"ok": True})
    remember(response, request)
    return response


async def log_out(request):
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE)
    return response


async def set_password(request):
    """Blank new turns the password off. Changing it needs the current one, even when signed in."""
    auth = request.app[auth_key]
    address = request.remote
    if auth.locked(address):
        return web.json_response({"error": "Too many tries. Wait a minute and try again."}, status=429)
    body = await request.json()
    new = str(body.get("new", ""))
    if auth.enabled and not await check_password(auth, str(body.get("current", ""))):
        auth.failed(address)
        return web.json_response({"error": "That isn't the current password."}, status=403)
    if new and len(new) < MIN_LENGTH:
        return web.json_response({"error": f"Use at least {MIN_LENGTH} characters."}, status=400)
    auth.passed(address)
    if new:
        await asyncio.get_running_loop().run_in_executor(pool, auth.set, new)
    else:
        auth.clear()
    response = web.json_response({"password": auth.enabled})
    if new:
        remember(response, request)
    return response


async def save_settings(request):
    app = request.app
    app[settings_key].update(await request.json())
    use_folders(app[settings_key])
    app[library_key].built = 0
    app[indexers_key].cache.clear()
    return web.json_response(app[settings_key].public())


async def test_settings(request):
    """Reads capabilities only, which doesn't count as a search on most indexers' limits."""
    app = request.app
    results = []
    for indexer in app[indexers_key].active():
        try:
            status_code, body = await app[indexers_key].call(indexer, t="caps")
            good = status_code == 200 and b"<caps" in body[:2000]
            results.append({"name": indexer["name"], "ok": good, "text": "Connected" if good else "Unexpected answer"})
        except SourceError as error:
            results.append({"name": indexer["name"], "ok": False, "text": str(error)})
    try:
        results.append({"name": "SABnzbd", "ok": True, "text": f"Connected, version {await app[sab_key].version()}"})
    except SourceError as error:
        results.append({"name": "SABnzbd", "ok": False, "text": str(error)})
    labels = (("Music folder", MUSIC), ("Import folder", IMPORT))
    for label, folder in labels:
        good = folder.is_dir() and can_write(folder)
        results.append({"name": label, "ok": good, "text": "Ready" if good else "Encore can't write here"})
    if DOWNLOADS == NOWHERE:
        results.append({"name": "Downloads folder", "ok": False, "text": "Not set"})
    if app[plex_key].server:
        state = await app[plex_key].status()
        text = {"ok": "Connected", "none": "Press Connect Plex at the top of the page"}.get(state, "Can't reach it")
        results.append({"name": "Plex", "ok": state == "ok", "text": text})
    return web.json_response({"results": results})


async def list_queue(request):
    return web.json_response({"jobs": [public(j) for j in request.app[queue_key].jobs]})


async def add_job(request):
    body = await request.json()
    album_id, key = str(body.get("album_id", "")), str(body.get("release", ""))
    if not ALBUM_ID.match(album_id):
        raise web.HTTPBadRequest(text="Bad album id")
    app = request.app
    queue = app[queue_key]
    if any(j["album_id"] == album_id and j["state"] in ACTIVE for j in queue.jobs):
        raise web.HTTPConflict(text="Already queued")
    offer = queue.offers.get(key)
    if not offer:
        raise web.HTTPConflict(text="That result has expired. Open the album again.")
    if not app[sab_key].ready:
        raise web.HTTPConflict(text="Add your SABnzbd details in Settings first")
    if not folders_ready():
        raise web.HTTPConflict(text="Set your folders in Settings first")
    album = await app[brainz_key].album(album_id)
    chose_other = "Not FLAC" in match.score(album, offer)["notes"]
    return web.json_response(public(queue.add(album, offer, bool(body.get("fix_artist")), chose_other)))


def find_job(request):
    job = request.app[queue_key].get(request.match_info["job"])
    if not job:
        raise web.HTTPNotFound()
    return job


async def retry_job(request):
    job, queue = find_job(request), request.app[queue_key]
    if job["state"] != "failed":
        raise web.HTTPConflict(text="Only failed albums can be retried")
    partial = request.path.endswith("/partial")
    keep_format = request.path.endswith("/import")
    if keep_format:
        job["any_format"] = True
    elif not partial:
        await queue.forget(job)
    reuse = bool((partial or keep_format) and job.get("kept"))
    queue.jobs.remove(job)
    queue.jobs.append(job)
    queue.update(job, state="queued", message="", percent=0, partial=partial, reuse=reuse)
    queue.wake.set()
    return web.json_response(public(job))


async def order_queue(request):
    """Reorders only what's waiting. Anything already running keeps its place, whatever the page sent."""
    queue = request.app[queue_key]
    body = await request.json()
    rank = {str(job_id): place for place, job_id in enumerate(body.get("ids") or [])}
    waiting = [j for j in queue.jobs if j["state"] == "queued"]
    arranged = iter(sorted(waiting, key=lambda j: rank.get(j["id"], len(rank))))
    queue.jobs = [next(arranged) if j["state"] == "queued" else j for j in queue.jobs]
    queue.update()
    return web.json_response({"ok": True})


async def remove_job(request):
    job, queue = find_job(request), request.app[queue_key]
    if queue.running is job:
        job["cancel"] = True
    else:
        await queue.forget(job)
        queue.jobs.remove(job)
        queue.update()
    return web.json_response({"ok": True})


async def clear_finished(request):
    queue = request.app[queue_key]
    queue.jobs = [j for j in queue.jobs if j["state"] != "done"]
    queue.update()
    return web.json_response({"ok": True})


async def plex_link(request):
    return web.json_response(await request.app[plex_key].start_link())


async def plex_check(request):
    return web.json_response({"linked": await request.app[plex_key].check_link(int(request.match_info["pin"]))})


async def index(request):
    return web.FileResponse(STATIC / "index.html")


@web.middleware
async def guard(request, handler):
    if request.method not in ("GET", "HEAD") and request.headers.get("X-Encore") != "1":
        raise web.HTTPForbidden(text="Missing request header")
    auth = request.app[auth_key]
    if auth.enabled and request.path not in OPEN_PATHS and not auth.valid(request.cookies.get(COOKIE)):
        if request.path.startswith("/api/"):
            return web.json_response({"error": "Sign in first"}, status=401)
        raise web.HTTPFound("/login")
    try:
        return await handler(request)
    except SourceError as error:
        return web.json_response({"error": str(error)}, status=502)


async def startup(app):
    app[owned_key].load(app[store_key])
    app["fill"] = asyncio.Event()
    app["worker"] = asyncio.create_task(app[queue_key].run())
    app["plex_rhythm"] = asyncio.create_task(plex_rhythm(app))
    app["filler"] = asyncio.create_task(fill_albums(app))


async def cleanup(app):
    for task in ("worker", "plex_rhythm", "filler"):
        app[task].cancel()
    await app[web_key].close()


def make_app():
    app = web.Application(middlewares=[guard])
    app[web_key] = Web()
    app[settings_key] = Settings(DATA / "settings.json")
    app[auth_key] = Auth(DATA / "auth.json")
    app[store_key] = Store(DATA / "encore.db")
    app[owned_key] = Owned()
    app[filling_key] = deque()
    app[deezer_key] = Deezer(app[web_key])
    app[brainz_key] = MusicBrainz(app[web_key], app[store_key])
    app[indexers_key] = Indexers(app[settings_key], app[web_key])
    use_folders(app[settings_key])
    app[sab_key] = Sabnzbd(app[settings_key], app[web_key])
    app[library_key] = Library()
    app[plex_key] = Plex(DATA / "plex.json", app[settings_key])


    async def filed():
        try:
            await sync_plex(app)
        except Exception as error:
            log.warning("Plex sync failed: %s", type(error).__name__)

    app[queue_key] = Queue(app[indexers_key], app[sab_key], app[brainz_key], app[library_key], app[plex_key],
                           app[store_key], filed)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/login", login_page)
    app.router.add_post("/api/login", log_in)
    app.router.add_post("/api/logout", log_out)
    app.router.add_post("/api/password", set_password)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/search", search)
    app.router.add_get("/api/album/{album}", album_page)
    app.router.add_get("/api/album/{album}/downloads", album_downloads)
    app.router.add_get("/api/artists", search_artists)
    app.router.add_get("/api/artist/{artist}", artist_page)
    app.router.add_get("/api/artist/{artist}/photo", artist_photo)
    app.router.add_get("/api/artist/{artist}/videos", artist_videos)
    app.router.add_post("/api/videos", add_video)
    app.router.add_get("/api/library", library_page)
    app.router.add_post("/api/library/sync", library_sync)
    app.router.add_get(r"/api/plex/thumb/{rating:\d+}", plex_thumb)
    app.router.add_get("/api/wanted", wanted_list)
    app.router.add_post("/api/wanted", add_wanted)
    app.router.add_delete("/api/wanted/{album}", remove_wanted)
    app.router.add_get("/api/queue", list_queue)
    app.router.add_post("/api/queue", add_job)
    app.router.add_post("/api/queue/clear", clear_finished)
    app.router.add_post("/api/queue/order", order_queue)
    app.router.add_post("/api/queue/{job}/retry", retry_job)
    app.router.add_post("/api/queue/{job}/partial", retry_job)
    app.router.add_post("/api/queue/{job}/import", retry_job)
    app.router.add_delete("/api/queue/{job}", remove_job)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings", save_settings)
    app.router.add_post("/api/settings/test", test_settings)
    app.router.add_post("/api/plex/link", plex_link)
    app.router.add_get(r"/api/plex/link/{pin:\d+}", plex_check)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    DATA.mkdir(exist_ok=True)
    PHOTOS.mkdir(exist_ok=True)

    async def build():
        return make_app()

    web.run_app(build(), host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
