"""Encore's engine: the queue that downloads, checks and files albums and videos, and the
library index it checks against. Everything here runs in plain threads.

An album only reaches the library once every track is present, is FLAC, decodes and fits a
MusicBrainz edition. Downloads land in their own SABnzbd category under DOWNLOADS, and nothing
outside that folder is ever deleted.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mutagen
from mutagen.flac import FLAC

from . import filing, match
from .config import DATA
from .filing import Hold, JobFailed, norm
from .sources import SourceError

JOBS_FILE = DATA / "jobs.json"
NOWHERE = Path("/nonexistent/encore")
MUSIC = IMPORT = DOWNLOADS = NOWHERE
MIN_FREE = 5 * 1024**3
INDEX_AGE = 300
POLL = 3
LOST_AFTER = 40
ACTIVE = ("queued", "downloading", "verifying", "filing", "scanning")
BUSY = ("verifying", "filing")            # a restart now would cut these short; downloads resume
AUDIO = (".flac", ".mp3", ".m4a", ".wav", ".ape", ".wv", ".aiff", ".aif", ".ogg", ".opus", ".wma", ".dsf", ".dff")
PROGRAMS = (".exe", ".scr", ".bat", ".cmd", ".com", ".msi", ".ps1", ".vbs", ".js", ".jar", ".lnk", ".dll")
COMPILATION = {"various artists", "various", "va"}
VIDEOS = (".mp4", ".mkv", ".avi", ".mpg", ".mpeg", ".vob", ".ts", ".m4v", ".mov", ".wmv")
VIDEO_SECONDS = 20
YOUTUBE_BYTES_PER_SECOND = 10**6        # 1080p H.264 runs about 0.4 MB a second; this leaves room
YOUTUBE_SLACK = 2                       # seconds a download may differ from the length YouTube gave

log = logging.getLogger("encore")


class Cancelled(Exception):
    pass


class Incomplete(JobFailed):
    """Tracks are missing, so the person can choose to take the rest."""


class NotFlac(JobFailed):
    """The release said FLAC and wasn't, so the person can choose to keep it anyway."""


class TrackFailed(JobFailed):
    """One YouTube track failed. The others stay downloaded, so a retry only fetches what's missing."""


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
        self.lock = threading.Lock()

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

    def fresh(self):
        """Rebuilt when it's older than INDEX_AGE; one thread builds while the rest wait for it."""
        if time.time() - self.built > INDEX_AGE:
            with self.lock:
                if time.time() - self.built > INDEX_AGE:
                    self.build()
        return self

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
                lossy = name.lower().endswith(".mp3") or getattr(info, "codec", "").startswith("mp4a")
                if bits and not lossy:                    # AAC reports 16 bits too, so only lossless shows them
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
    """One job at a time, in its own thread. The page's threads read and change the job list
    too, so every change to it, and every save, happens under `lock`."""

    def __init__(self, indexers, sab, brainz, library, plex, store, on_filed=None, youtube=None):
        self.store = store
        self.youtube = youtube
        self.on_filed = on_filed
        self.indexers = indexers
        self.sab = sab
        self.brainz = brainz
        self.library = library
        self.plex = plex
        self.jobs = []
        self.offers = {}
        self.lock = threading.RLock()
        self.wake = threading.Event()
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

    def busy(self):
        """What /healthz reports: jobs a restart now would cut short."""
        with self.lock:
            return [f"{j['artist']} - {j['title']}" for j in self.jobs if j["state"] in BUSY]

    def forget(self, job):
        """Removes one job's download from SABnzbd and the share. The worst this costs is a fresh download."""
        with self.lock:
            job["kept"] = job["reuse"] = False
            nzo, job["nzo"] = job.get("nzo"), None
            storage, job["storage"] = job.get("storage"), None
        if nzo:
            self.sab.forget(nzo)
        if storage and inside_downloads(storage):
            shutil.rmtree(storage, True)

    def save(self):
        with self.lock:
            temp = JOBS_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps(self.jobs, indent=1))
            temp.replace(JOBS_FILE)

    def get(self, job_id):
        with self.lock:
            return next((j for j in self.jobs if j["id"] == job_id), None)

    def add(self, album, offer, fix_artist, any_format=False):
        job = {key: album[key] for key in ("title", "artist", "year", "tracks", "cover")}
        job.update(album_id=album["id"], id=uuid.uuid4().hex[:12], state="queued", percent=0, message="",
                   fix_artist=fix_artist, folder="", quality="", added=time.time(), partial=False, any_format=any_format,
                   source={k: offer[k] for k in ("indexer", "guid", "title", "size", "key")})
        with self.lock:
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
        with self.lock:
            self.jobs.append(job)
            self.update()
        self.wake.set()
        return job

    def add_youtube(self, album, edition, tracks, partial):
        """tracks: [{key, disc, number, title, video, seconds, channel}], the linked tracks of one edition."""
        job = {key: album[key] for key in ("title", "artist", "year", "cover")}
        job.update(album_id=album["id"], id=uuid.uuid4().hex[:12], kind="youtube", state="queued", percent=0,
                   message="", step="", fix_artist=True, folder="", quality="", added=time.time(), partial=partial,
                   tracks=len(tracks), source={"type": "youtube", "edition": edition, "tracks": tracks})
        with self.lock:
            self.jobs.append(job)
            self.update()
        self.wake.set()
        return job

    def update(self, job=None, **changes):
        with self.lock:
            if job:
                job.update(changes)
            self.save()

    def start(self):
        """Runs the queue in a daemon thread. Jobs that were scanning carry on waiting for Plex."""
        for job in list(self.jobs):
            if job["state"] == "scanning":
                threading.Thread(target=self.announce, args=(job,), daemon=True).start()
        threading.Thread(target=self.run, name="encore-queue", daemon=True).start()

    def next_job(self):
        with self.lock:
            return next((j for j in self.jobs if j["state"] == "queued"), None)

    def run(self):
        while True:
            job = self.next_job()
            if not job:
                self.wake.clear()
                if not self.next_job():          # added between the look and the clear
                    self.wake.wait()
                continue
            self.running = job
            try:
                self.process_job(job)
                self.finish(job)
            except Cancelled:
                self.update(job, state="failed", message="Cancelled.")
            except SourceError as error:
                self.update(job, state="failed", message=f"{error}.")
            except Incomplete as error:
                self.update(job, state="failed", message=str(error), can_partial=True, kept=True)
            except NotFlac as error:
                self.update(job, state="failed", message=str(error), can_import=True, kept=True)
            except TrackFailed as error:
                self.update(job, state="failed", message=str(error), kept=True)
            except JobFailed as error:
                self.update(job, state="failed", message=str(error))
            except Exception:
                log.exception("Job failed")
                self.update(job, state="failed", message="Something went wrong. See the Encore log.")
            finally:
                if job.get("state") != "scanning" and not job.get("kept"):
                    self.forget(job)
                    self.update()
                self.running = None

    def result(self, job, message):
        notes = [job.get("partial_note"), job.get("edition_note"), job.get("video_note")]
        return " ".join([message, *filter(None, notes)])

    def finish(self, job):
        self.store.unwant(job["album_id"])
        if job["where"] == "import" and job.get("videos_filed") and self.plex.linked:
            threading.Thread(target=self.scan_quietly, args=(job["videos_filed"][0].split("/")[0],), daemon=True).start()
        if job["where"] == "import":
            self.update(job, state="done", message=self.result(job, job["reason"]), finished=time.time())
        elif self.plex.linked:
            self.update(job, state="scanning", message="Plex is scanning.")
            threading.Thread(target=self.announce, args=(job,), daemon=True).start()
        else:
            self.update(job, state="done", finished=time.time(),
                        message=self.result(job, "In your library. Connect Plex so it scans by itself."))

    def scan_quietly(self, folder):
        try:
            self.plex.scan(folder)
        except Exception as error:
            log.warning("Plex scan failed: %s", type(error).__name__)

    def video_in_plex(self, job):
        self.plex.scan(job["folder"].split("/")[0])
        row = self.store.plex_artist(job["artist_id"])
        if not row:
            return False
        return self.plex.video_shown(row["rating"], job["folder"].split("/")[-1])

    def announce(self, job):
        try:
            if job.get("kind") == "video":
                shown = self.video_in_plex(job)
            else:
                # A YouTube album's videos sit in the artist folder, so that's what Plex scans.
                self.plex.scan(job["folder"].split("/")[0] if job.get("kind") == "youtube" else job["folder"])
                shown = self.plex.confirm(job["artist"], job["folder"], job["tracks"])
            message = "In your library and Plex." if shown else "In your library, but Plex hasn't shown it yet."
        except Exception as error:
            log.warning("Plex scan failed: %s", type(error).__name__)
            message = "In your library, but the Plex scan failed. Run Scan Library Files."
        self.forget(job)
        self.update(job, state="done", message=self.result(job, message), finished=time.time())
        if self.on_filed:
            self.on_filed()

    def check_cancel(self, job):
        with self.lock:
            if job.get("cancel"):
                job.pop("cancel")
                raise Cancelled

    def process_job(self, job):
        with self.lock:
            job["kept"] = False
            for key in ("partial_note", "edition_note", "video_note", "videos_filed", "can_partial", "can_import"):
                job.pop(key, None)
        self.update(job, state="downloading", message="")
        if job.get("kind") == "youtube":
            return self.process_youtube(job)
        if job.get("kind") == "video":
            source = self.download(job)
            self.check_cancel(job)
            self.update(job, state="verifying", message="Checking the video.")
            chosen = prepare_video(source, job)
            self.update(job, state="filing", message="Filing the video.")
            self.update(job, where="library", folder=filing.to_artist(chosen, job["artist"], job["title"], MUSIC, job["id"]))
            return
        album = self.brainz.album(job["album_id"])
        with self.lock:
            reuse = job.pop("reuse", False)
        if reuse and job.get("storage"):
            source = Path(job["storage"])
        else:
            source = self.download(job)
        self.check_cancel(job)
        self.update(job, state="verifying", message="Checking every track.")
        hold = prepare(source, album, job)
        self.check_cancel(job)
        self.update(job, state="filing", message="Filing the album.")
        self.update(job, **file_album(source, job["id"], self.library, hold))

    def process_youtube(self, job):
        album = self.brainz.album(job["album_id"])
        edition = next((e for e in album["editions"] if e["id"] == job["source"]["edition"]), None)
        if not edition:
            raise JobFailed("That edition has gone from MusicBrainz. Open the album and build it again.")
        listed = {(t["disc"], t["number"]): t for t in edition["tracks"]}
        tracks = job["source"]["tracks"]
        if any((t["disc"], t["number"]) not in listed for t in tracks):
            raise JobFailed("The track list has changed on MusicBrainz. Open the album and build it again.")
        folder = DOWNLOADS / "encore-youtube" / job["id"]
        make_folder(DOWNLOADS)
        need = sum(t.get("seconds") or 300 for t in tracks) * YOUTUBE_BYTES_PER_SECOND
        if shutil.disk_usage(DOWNLOADS).free < need * 2 + MIN_FREE:
            raise JobFailed("Not enough free space on the share for this album.")
        self.update(job, storage=str(folder))
        videos = self.fetch_videos(job, tracks, folder)
        self.check_cancel(job)
        self.update(job, state="verifying", message="Checking every track.", step="", percent=100)
        album_dir = folder / "album"
        shutil.rmtree(album_dir, ignore_errors=True)
        album_dir.mkdir(parents=True)
        pairs, off = [], 0
        for track in tracks:
            self.check_cancel(job)
            listed_track = listed[(track["disc"], track["number"])]
            seconds = youtube_audio(videos[track["key"]], album_dir / f"{track['disc']}-{track['number']:03d}.m4a",
                                    track_label(track), track.get("seconds"))
            pairs.append(({"path": album_dir / f"{track['disc']}-{track['number']:03d}.m4a"}, listed_track))
            off += match.youtube_fit(listed_track["seconds"], seconds) == "off"
        linked = {(t["disc"], t["number"]) for t in tracks}
        gone = [t["title"] for t in edition["tracks"] if (t["disc"], t["number"]) not in linked]
        if gone:
            job["partial_note"] = f"Missing {and_more(gone)}."
        if off:
            job["edition_note"] = f"{count(off)} differ{'s' if off == 1 else ''} in length from the album."
        write_tags(pairs, album, edition, True)
        add_cover(self.brainz.web, album["id"], album_dir, [item["path"] for item, _ in pairs])
        rates = [getattr(mutagen.File(item["path"]).info, "bitrate", 0) for item, _ in pairs]
        job["quality"] = f"YouTube AAC {min(rates) // 1000}" if rates and min(rates) else "YouTube AAC"
        job["tracks"] = len(pairs)
        self.check_cancel(job)
        self.update(job, state="filing", message="Filing the album.")
        self.update(job, **file_album(album_dir, job["id"], self.library))
        self.file_videos(job, album, tracks, listed, videos)

    def fetch_videos(self, job, tracks, folder):
        """Downloads each track's video, skipping any already here from an earlier try."""
        videos = {}
        for place, track in enumerate(tracks):
            self.check_cancel(job)
            share = 100 / len(tracks)
            self.update(job, step=f"Track {place + 1} of {len(tracks)}", percent=round(place * share, 1))
            path = folder / f"{track['video']}.mp4"
            if not path.exists():
                shown = [0.0]

                def progress(value, base=place * share):
                    overall = round(base + value * share / 100, 1)
                    if overall - shown[0] >= 1:
                        shown[0] = overall
                        self.update(job, percent=overall)

                try:
                    path = self.youtube.download(track["video"], folder, progress, lambda: job.get("cancel"))
                except SourceError as error:
                    raise TrackFailed(f"{track_label(track)}: {error}. Change its link and retry.") from None
                if path is None:
                    self.check_cancel(job)
                    raise Cancelled
            videos[track["key"]] = path
        return videos

    def file_videos(self, job, album, tracks, listed, videos):
        """Each track's video goes in the artist folder, unless it was only the album audio over a still."""
        filed, there, still = [], 0, 0
        compilation = norm(album["artist"]) in COMPILATION
        for track in tracks:
            if match.audio_only(track.get("channel")):
                still += 1
                continue
            listed_track = listed[(track["disc"], track["number"])]
            artist = (listed_track["artist"] or album["artist"]) if compilation else album["artist"]
            where = filing.to_artist(videos[track["key"]], artist, listed_track["title"], MUSIC, job["id"], skip_existing=True)
            if where:
                filed.append(where)
            else:
                there += 1
        parts = [f"{len(filed)} video{'' if len(filed) == 1 else 's'} filed" if filed else "",
                 f"{there} already there" if there else "",
                 f"{still} audio only" if still else ""]
        note = ", ".join(p for p in parts if p)
        self.update(job, videos_filed=filed, video_note=f"{note[0].upper()}{note[1:]}." if note else "")

    def download(self, job):
        size = job["source"]["size"]
        make_folder(DOWNLOADS)
        if shutil.disk_usage(DOWNLOADS).free < size * 2 + MIN_FREE:
            raise JobFailed("Not enough free space on the share for this album.")
        if not job.get("nzo"):
            nzb = self.indexers.fetch(job["source"]["indexer"], job["source"]["guid"])
            name = filing.safe(f"{job['artist']} - {job['title']} [{job['id']}]")
            self.update(job, nzo=self.sab.add(nzb, name), percent=0)
        return self.wait(job)

    def wait(self, job):
        silent = 0
        while True:
            self.check_cancel(job)
            try:
                state = self.sab.state(job["nzo"])
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
            time.sleep(POLL)


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


def track_label(track):
    where = f"Disc {track['disc']} track {track['number']}" if track["disc"] > 1 else f"Track {track['number']}"
    return f"{where} (“{track['title']}”)"


def youtube_audio(video, target, label, expected):
    """Checks a downloaded video reads through to the end, then copies its sound out as the album's track.
    YouTube's sound is AAC, which goes in an .m4a as it is; anything else is converted once."""
    info = probe(video)
    sound = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    seconds = float((info.get("format") or {}).get("duration") or 0)
    read = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-map", "0", "-c", "copy", "-f", "null", "-"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not sound or not seconds or read.returncode != 0 or (expected and abs(seconds - expected) > YOUTUBE_SLACK):
        video.unlink(missing_ok=True)
        raise TrackFailed(f"{label}: the download was damaged or cut short. Retry to fetch it again.")
    codec = ["-c:a", "copy"] if sound.get("codec_name") == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    made = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(video), "-map", "0:a:0", "-vn", *codec,
                           "-movflags", "+faststart", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if made.returncode != 0 or not decode_ok(target):
        raise JobFailed(f"{label}: Encore couldn't take the sound out of the video.")
    return seconds


def add_cover(web, album_id, folder, files):
    """The front cover from the Cover Art Archive, as cover.jpg and inside each track. Optional."""
    from mutagen.mp4 import MP4, MP4Cover
    from .sources import cover
    try:
        response = web.get(cover(album_id, 1200))
        if response.status_code != 200 or not response.content.startswith(b"\xff\xd8"):
            return
    except Exception as error:
        log.warning("Cover fetch failed: %s", type(error).__name__)
        return
    (folder / "cover.jpg").write_bytes(response.content)
    for path in files:
        audio = MP4(path)
        audio["covr"] = [MP4Cover(response.content, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()


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




def public(job):
    return {k: v for k, v in job.items() if k not in ("cancel", "fix_artist", "reuse", "nzo", "storage")}

