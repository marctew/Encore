"""Works out where a verified album belongs and copies it there.

Albums go to Artist/Album (Year), with tracks named by number and title. Anything that
needs a person to look at it raises Hold, and the album goes to the import folder instead.
"""
import hashlib
import os
import re
import shutil
import time
import unicodedata
from collections import Counter
from pathlib import Path

import mutagen

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TAGGED = (".flac", ".mp3", ".m4a", ".ogg", ".opus")
IMAGES = (".jpg", ".jpeg", ".png")
OTHER_AUDIO = (".wav", ".ape", ".wv", ".aiff", ".aif", ".wma", ".dsf", ".dff")
KEPT = (*TAGGED, *IMAGES, *OTHER_AUDIO)


class Hold(Exception):
    """The album is fine but needs a person to decide where it goes."""


class JobFailed(Exception):
    pass


def norm(text):
    text = unicodedata.normalize("NFKD", text).casefold().replace("&", " and ")
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", re.sub(r"^the\s+", "", text.strip())).strip()


def safe(name):
    """Strips leading dots too, because folders starting with a dot are hidden from Plex."""
    return " ".join(ILLEGAL.sub("-", name).split()).strip(" .")


def number(value):
    match = re.match(r"\d+", str(value or ""))
    return int(match.group()) if match else None


def read_track(path):
    audio = mutagen.File(path, easy=True)
    first = lambda key: (audio.get(key) or [None])[0]
    return {"path": path, "artist": first("albumartist") or first("artist"), "album": first("album"),
            "title": first("title"), "date": first("date"), "track": number(first("tracknumber")),
            "disc": number(first("discnumber")) or 1}


def audio_in(folder):
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in TAGGED)


def plan(source, music, library):
    """Returns the album's target folder and a {source file: new name} map, or raises Hold."""
    tracks = [read_track(p) for p in audio_in(source)]
    if any(not (t["artist"] and t["album"] and t["title"]) for t in tracks):
        raise Hold("Some tags are missing, so it's here for you to file.")
    if any(t["track"] is None for t in tracks):
        raise Hold("Some track numbers are missing, so it's here for you to file.")
    if len({norm(t["artist"]) for t in tracks}) > 1 or len({norm(t["album"]) for t in tracks}) > 1:
        raise Hold("Its tags disagree on the artist or album, so it's here for you to file.")
    artist, album = tracks[0]["artist"], tracks[0]["album"]
    owned = library.find(artist, album)
    if owned:
        have = f" ({owned['quality']})" if owned["quality"] else ""
        label = "You already have" if owned["match"] == "exact" else "This may match"
        raise Hold(f"{label} {owned['folder']}{have}, so it's here to compare.")
    tops = {norm(d.name): d.name for d in os.scandir(music) if d.is_dir()}
    top = tops.get(norm(artist)) or safe(artist)
    years = Counter(str(t["date"])[:4] for t in tracks if t["date"])
    year = years.most_common(1)[0][0] if years else ""
    name = safe(f"{album} ({year})" if year else album)
    if not top or not name:
        raise Hold("Its name can't be made into a folder, so it's here for you to file.")
    target = music / top / name
    if target.parent.exists() and name.casefold() in (d.name.casefold() for d in os.scandir(target.parent)):
        raise Hold(f"{top}/{name} already exists, so it's here to compare.")
    multi = len({t["disc"] for t in tracks}) > 1
    names = {}
    for t in tracks:
        prefix = f"{t['disc']}-" if multi else ""
        names[t["path"]] = f"{prefix}{t['track']:02d} - {safe(t['title'])[:120].rstrip('. ')}{t['path'].suffix.lower()}"
    if len({n.casefold() for n in names.values()}) != len(names):
        raise Hold("Two tracks would get the same file name, so it's here for you to file.")
    return target, names


BLOCK = 4 << 20


def sha256(path, moved=None):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while block := source.read(BLOCK):
            digest.update(block)
            if moved:
                moved(len(block))
    return digest.hexdigest()


class Progress:
    """Reports a copy as it goes: copying counts once, checking reads both sides, so that counts twice.
    report(text, percent) is called at most every EVERY seconds."""
    EVERY = 2

    def __init__(self, report, pairs):
        self.report = report
        self.total = max(1, sum(source.stat().st_size for source, _ in pairs) * 3)
        self.done, self.shown = 0, 0.0

    def moved(self, count, text):
        self.done += count
        if self.report and time.monotonic() - self.shown >= self.EVERY:
            self.shown = time.monotonic()
            self.report(text, round(min(99.0, self.done * 100 / self.total), 1))


def copy_checked(pairs, report=None):
    """Copies each file, then reads both sides back and compares, because the share can drop a write."""
    progress = Progress(report, pairs)
    count = len(pairs)
    for place, (source, target) in enumerate(pairs, 1):
        target.parent.mkdir(parents=True, exist_ok=True)
        text = f"Copying {place} of {count} file{'' if count == 1 else 's'}"
        with open(source, "rb") as reading, open(target, "wb") as writing:
            while block := reading.read(BLOCK):
                writing.write(block)
                progress.moved(len(block), text)
    for place, (source, target) in enumerate(pairs, 1):
        text = f"Checking {place} of {count} cop{'y' if count == 1 else 'ies'}"
        if sha256(source, lambda n: progress.moved(n, text)) != sha256(target, lambda n: progress.moved(n, text)):
            raise JobFailed(f"{source.name} changed while copying to the share.")


def open_up(folder):
    """Leaves the album editable from Windows, as the share's other files are."""
    for path in [folder, *folder.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)


def to_library(source, target, names, music, job_id, report=None):
    """Builds the album under a dot folder Plex can't see, then renames it into place."""
    temp = music / f".encore-incoming-{job_id}"
    pairs = [(path, temp / name) for path, name in names.items()]
    used = {name.casefold() for name in names.values()}
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix.lower() in IMAGES and path.name.casefold() not in used:
            used.add(path.name.casefold())
            pairs.append((path, temp / path.name))
    try:
        copy_checked(pairs, report)
        open_up(temp)
        if not target.parent.exists():
            target.parent.mkdir()
            target.parent.chmod(0o777)
        temp.rename(target)
    except OSError as error:
        raise JobFailed(f"Couldn't copy to the share: {error.strerror}.") from None
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def to_artist(source, artist, song, music, job_id, skip_existing=False):
    """Files a music video in the artist's folder, named the way Plex looks for. Never overwrites:
    with skip_existing, a video that's already there returns None instead of failing the job."""
    tops = {norm(d.name): d.name for d in os.scandir(music) if d.is_dir()}
    top = tops.get(norm(artist)) or safe(artist)
    name = safe(f"{song}-video{source.suffix.lower()}")
    folder = music / top
    target = folder / name
    if not top or not name:
        raise JobFailed("Its name can't be made into a file name.")
    if target.exists():
        if skip_existing:
            return None
        raise JobFailed(f"You already have {name}.")
    temp = music / f".encore-incoming-{job_id}"
    try:
        copy_checked([(source, temp / name)])
        if not folder.exists():
            folder.mkdir()
            folder.chmod(0o777)
        (temp / name).chmod(0o666)
        (temp / name).rename(target)
    except OSError as error:
        raise JobFailed(f"Couldn't copy to the share: {error.strerror}.") from None
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return f"{top}/{name}"


def to_import(source, imports, job_id, report=None):
    temp = imports / f"_encore-incoming-{job_id}"
    pairs = [(p, temp / p.relative_to(source)) for p in source.rglob("*") if p.is_file() and p.suffix.lower() in KEPT]
    try:
        copy_checked(pairs, report)
        open_up(temp)
        name = safe(source.name)
        target, count = imports / name, 2
        while target.exists():
            target, count = imports / f"{name} ({count})", count + 1
        temp.rename(target)
    except OSError as error:
        raise JobFailed(f"Couldn't copy to the share: {error.strerror}.") from None
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return target.name
