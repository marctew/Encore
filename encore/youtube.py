"""YouTube through yt-dlp, for albums Usenet doesn't have.

yt-dlp runs as its own program, never imported, so an upgrade takes effect without a restart and
a crash in it can't take the worker down. Nothing typed on the page ever reaches its command line:
links are cut down to an 11-character video id and rebuilt, and every URL goes after `--`.
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .sources import SourceError

log = logging.getLogger("encore")
BIN = Path(sys.executable).parent
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
SORT = "res:1080,vcodec:h264,acodec:m4a"
LOOKUP_SECONDS = 45
CACHE_AGE = 7 * 86400
UPDATE_SECONDS = 600
PERCENT = re.compile(r"^\s*(\d+(?:\.\d+)?)%\s*$")
# "0:00 Song", "1. Song - 3:45", "[12:03] Song": a timestamp anywhere on a line of the description.
STAMP = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")
# yt-dlp's errors, in the words the page shows. First match wins.
REASONS = (
    ("confirm your age", "That video is age-restricted, which Encore can't download yet"),
    ("not a bot", "YouTube is blocking downloads from this server for now. Try again later"),
    ("private video", "That video is private"),
    ("members-only", "That video is for channel members only"),
    ("copyright", "That video has been taken down"),
    ("not available in your country", "That video isn't available in this country"),
    ("video unavailable", "That video has been removed or made private"),
    ("http error 429", "YouTube is limiting requests from this server. Try again later"),
    ("http error 403", "YouTube refused the download. Update yt-dlp in Settings, or try again later"),
    ("unable to download webpage", "YouTube didn't answer. Try again shortly"),
)


def youtube_id(text):
    """The video id in a YouTube link (or a bare id), or None for anything else."""
    text = str(text or "").strip()
    if VIDEO_ID.match(text):
        return text
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if host not in HOSTS:
        return None
    path = [p for p in parts.path.split("/") if p]
    if host.endswith("youtu.be"):
        candidate = path[0] if len(path) == 1 else ""
    elif path == ["watch"]:
        candidate = (parse_qs(parts.query).get("v") or [""])[0]
    elif len(path) == 2 and path[0] in ("shorts", "live", "embed", "v"):
        candidate = path[1]
    else:
        return None
    return candidate if VIDEO_ID.match(candidate) else None


def watch_url(video_id):
    if not VIDEO_ID.match(video_id):
        raise SourceError("That isn't a YouTube video")
    return f"https://www.youtube.com/watch?v={video_id}"


def summary(entry):
    video_id = entry.get("id") or ""
    return {"id": video_id, "title": entry.get("title") or "Untitled", "seconds": int(entry.get("duration") or 0),
            "channel": entry.get("channel") or entry.get("uploader") or "",
            "thumb": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
            "live": entry.get("live_status") in ("is_live", "is_upcoming"),
            "year": str(entry.get("release_year") or entry.get("upload_date") or "")[:4],
            "chapters": chapters(entry)}


def chapters(entry):
    """[{start, title}] in order: YouTube's own chapters, or else the timestamps in the description,
    which is where uploads of a whole album usually list their tracks."""
    seconds = entry.get("duration") or 0
    found = [{"start": float(c.get("start_time") or 0), "title": str(c.get("title") or "").strip()}
             for c in entry.get("chapters") or [] if isinstance(c, dict)]
    if not found:
        found = description_chapters(entry.get("description") or "", seconds)
    return [c for c in found if not seconds or c["start"] < seconds]


def description_chapters(text, seconds=0):
    """Timestamps on lines of their own, one per line, rising from near 0:00. Fewer than three, out of
    order, or starting later (a list of track lengths, "Song (3:45)"), and it isn't a list of starts."""
    found = []
    for line in text.splitlines():
        stamps = list(STAMP.finditer(line))
        if len(stamps) != 1:
            continue
        stamp = stamps[0]
        start = int(stamp.group(1) or 0) * 3600 + int(stamp.group(2)) * 60 + int(stamp.group(3))
        title = (line[:stamp.start()] + " " + line[stamp.end():]).strip()
        title = re.sub(r"^[\s\-–—|:.)\]\[(]*(\d{1,2}[.)]\s+)?", "", title)
        title = re.sub(r"[\s\-–—|:(\[]*$", "", title).strip()
        found.append({"start": float(start), "title": title})
    if len(found) < 3 or found[0]["start"] > 15 or any(b["start"] <= a["start"] for a, b in zip(found, found[1:])):
        return []
    if seconds and found[-1]["start"] >= seconds:
        return []
    return found


def reason(output):
    text = (output or "").casefold()
    for clue, words in REASONS:
        if clue in text:
            return words
    lines = [line for line in (output or "").splitlines() if line.strip()]
    log.warning("yt-dlp failed: %s", lines[-1][:300] if lines else "no output")
    return "YouTube didn't answer. Try again shortly"


class YouTube:
    """Lookups and searches take turns, so the page never has several running against YouTube.
    Downloads run outside that lock, so a long album doesn't hold up pasting links for the next."""

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()

    @staticmethod
    def command(*args):
        program = os.environ.get("ENCORE_YTDLP") or str(BIN / "yt-dlp")
        line = [program, "--ignore-config", "--no-playlist"]
        deno = BIN / "deno"
        if deno.exists():
            line += ["--js-runtimes", f"deno:{deno}"]
        return [*line, *args]

    def run(self, *args):
        with self.lock:
            try:
                done = subprocess.run(self.command(*args), capture_output=True, text=True, timeout=LOOKUP_SECONDS,
                                      stdin=subprocess.DEVNULL)
            except FileNotFoundError:
                raise SourceError("yt-dlp isn't installed. Run scripts/update.sh") from None
            except subprocess.TimeoutExpired:
                raise SourceError("YouTube didn't answer. Try again shortly") from None
        if done.returncode:
            raise SourceError(reason(done.stderr))
        try:
            return json.loads(done.stdout)
        except ValueError:
            raise SourceError("YouTube sent something Encore can't read") from None

    def lookup(self, video_id, fresh=False):
        saved = None if fresh else self.store.youtube_video(video_id)
        if saved and time.time() - saved["saved_at"] < CACHE_AGE:
            return saved["video"]
        video = summary(self.run("-J", "--skip-download", "--", watch_url(video_id)))
        if video["id"] != video_id:
            raise SourceError("YouTube sent a different video")
        self.store.save_youtube_video(video)
        return video

    def search(self, text, count=8):
        found = self.run("-J", "--flat-playlist", "--", f"ytsearch{count}:{text}")
        return [summary(e) for e in found.get("entries") or [] if VIDEO_ID.match(e.get("id") or "")]

    def download(self, video_id, folder, progress=None, cancelled=None, audio=False):
        """Downloads one video as mp4 into folder and returns its path. The video and its audio come
        down separately and are merged, so the progress goes 0-100 twice; it's reported as one run.
        With audio, only the sound (an .m4a when YouTube has one, as it nearly always does)."""
        folder.mkdir(parents=True, exist_ok=True)
        wanted = ["-f", "ba[ext=m4a]/ba"] if audio else ["-S", SORT, "--merge-output-format", "mp4"]
        line = self.command("--newline", "--no-mtime", "--retries", "5", "--fragment-retries", "5",
                            *wanted, "-o", str(folder / "%(id)s.%(ext)s"),
                            "--progress-template", "download:%(progress._percent_str)s", "--", watch_url(video_id))
        try:
            process = subprocess.Popen(line, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise SourceError("yt-dlp isn't installed. Run scripts/update.sh") from None
        stopped = threading.Event()

        def watch():
            while process.poll() is None:
                if cancelled and cancelled():
                    stopped.set()
                    process.kill()
                    return
                time.sleep(0.5)

        threading.Thread(target=watch, daemon=True).start()
        tail, part, last = deque(maxlen=30), 0, 0.0
        for text in process.stdout:
            tail.append(text)
            found = PERCENT.match(text)
            if not found:
                continue
            value = float(found.group(1))
            if value < last - 50:
                part += 1
            last = value
            if progress:
                progress(min(99.0, value if audio else (min(part, 1) * 100 + value) / 2))
        process.wait()
        if stopped.is_set():
            return None
        if process.returncode:
            raise SourceError(reason("".join(tail)))
        if audio:
            sound = [p for p in folder.glob(f"{video_id}.*") if p.suffix not in (".part", ".ytdl", ".json", ".tmp")]
            if not sound:
                raise SourceError("YouTube didn't send any sound Encore can use")
            return sound[0]
        video = folder / f"{video_id}.mp4"
        if not video.exists():
            raise SourceError("YouTube didn't send a video Encore can use")
        return video

    def version(self):
        try:
            done = subprocess.run(self.command("--version"), capture_output=True, text=True, timeout=30,
                                  stdin=subprocess.DEVNULL)
            return done.stdout.strip() if done.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def update(self):
        """Upgrades yt-dlp (and Deno, which it needs for YouTube) in Encore's own venv."""
        try:
            done = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default]", "deno"],
                                  capture_output=True, text=True, timeout=UPDATE_SECONDS, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise SourceError("The update took too long. Try again later") from None
        if done.returncode:
            log.warning("yt-dlp update failed: %s", (done.stderr or done.stdout)[-500:])
            raise SourceError("The update failed. See the Encore log")
        return self.version()
