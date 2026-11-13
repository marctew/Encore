"""Encore's web side: the Flask app, its sign-in, and the JSON API the page calls.

create_app() builds a fresh app with its own services (the tests make several); start_workers()
starts the queue and the background jobs, once, in the process that serves requests.
"""
import logging
import re
import shutil
import threading
import time
from collections import Counter, deque
from types import SimpleNamespace

from urllib.parse import quote

from flask import Blueprint, Flask, Response, current_app, jsonify, redirect, render_template, request, send_file, url_for
from markupsafe import Markup
from werkzeug.http import HTTP_STATUS_CODES
from werkzeug.middleware.proxy_fix import ProxyFix

from . import __version__, engine, filing, match, split
from .auth import MIN_LENGTH, SESSION_DAYS, Auth
from .config import DATA, PHOTOS, TRUSTED_PROXY
from .engine import ACTIVE, Library, Owned, Queue, album_key, folders_ready, public, use_folders
from .filing import norm
from .plex import Plex
from .sources import Deezer, Indexers, MusicBrainz, Sabnzbd, Settings, SourceError, Web
from .store import Store
from .youtube import YouTube, playlist_id, youtube_id

PLEX_SYNC = 3600
PHOTO_RETRY = 30 * 86400
ARTIST_LIMIT = 12
COOKIE = "encore_session"
OPEN_PATHS = ("/login", "/api/login", "/healthz", "/favicon.ico")
# Plain form posts (the sidebar's Sign out). Signing out is the only one, and it can't harm anything.
FORM_POSTS = ("/logout",)
ALBUM_ID = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
ORDER = {"good": 0, "maybe": 1, "no": 2}

log = logging.getLogger("encore")
bp = Blueprint("encore", __name__)


class Refusal(Exception):
    """An answer for the page in plain text, like aiohttp's HTTPException(text=...)."""

    def __init__(self, status, text=""):
        super().__init__(text)
        self.status, self.text = status, text


def svc():
    return current_app.extensions["encore"]


def checked_id(value, what="album"):
    if not ALBUM_ID.match(value):
        raise Refusal(400, f"Bad {what} id")
    return value


def body():
    return request.get_json(force=True, silent=True) or {}


# ---------------------------------------------------------------- owned albums and Plex

def mark_owned(albums):
    s = svc()
    library = s.library.fresh()
    owned, wanted = s.owned, s.store.wanted_ids()
    for album in albums:
        album["owned"] = library.find(album["artist"], album["title"])
        held = owned.find(album)
        album["plex_url"] = s.plex.web_url(held["rating"]) if held else ""
        if held and not album["owned"]:
            album["owned"] = {"match": "exact", "folder": f"{held['title']} in Plex", "quality": None}
        album["wanted"] = album["id"] in wanted


def sync_plex(s):
    """Refreshes what Plex holds, and anything wanted that has since arrived stops being wanted."""
    artists, albums = s.plex.library()
    s.store.replace_plex(artists, albums)
    s.owned.load(s.store)
    library = s.library.fresh()
    for album in s.store.wanted():
        found = library.find(album["artist"], album["title"])
        if s.owned.find(album) or (found and found["match"] == "exact"):
            s.store.unwant(album["id"])


def plex_rhythm(s):
    while True:
        if s.plex.linked and s.plex.server:
            try:
                sync_plex(s)
            except Exception as error:
                log.warning("Plex sync failed: %s", type(error).__name__)
        time.sleep(PLEX_SYNC)


def fill_albums(s):
    """Fetches track lists ahead of time, one at a time, stepping aside whenever the page needs MusicBrainz."""
    while True:
        if not s.filling:
            s.fill.clear()
            if not s.filling:
                s.fill.wait()
            continue
        album_id = s.filling.popleft()
        while s.brainz.busy:
            time.sleep(0.5)
        try:
            s.brainz.album(album_id, background=True)
        except SourceError:
            time.sleep(30)
        except Exception as error:
            log.warning("Track list fetch failed: %s", type(error).__name__)


# ---------------------------------------------------------------- finding music

@bp.get("/api/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify(albums=[])
    store = svc().store
    ids, saved_at = store.search("album", norm(query))
    albums = [store.album(i) for i in ids] if ids is not None and "refresh" not in request.args else None
    if albums is None or None in albums:
        albums = svc().brainz.search(query)
        for album in albums:
            store.save_album(album)
        store.save_search("album", norm(query), [a["id"] for a in albums])
        saved_at = time.time()
    mark_owned(albums)
    return jsonify(albums=albums, saved_at=saved_at)


@bp.get("/api/album/<album>")
def album_page(album):
    details = dict(svc().brainz.album(checked_id(album), refresh="refresh" in request.args))
    details["editions"] = len(details["editions"])
    mark_owned([details])
    return jsonify(details)


def offer_row(item, verdict, failed):
    return {"key": item["key"], "title": item["title"], "indexer": item["indexer"], "size": item["size"],
            "posted": item["posted"], "failed": failed, **verdict}


def failed_offers(queue):
    with queue.lock:
        return {j["source"]["key"]: j["message"] for j in queue.jobs if j["state"] == "failed" and j.get("source")}


@bp.get("/api/album/<album>/downloads")
def album_downloads(album):
    s = svc()
    checked_id(album)
    if not s.indexers.active():
        raise SourceError("Add your indexer API keys in Settings first")
    details = s.brainz.album(album)
    items, problems = s.indexers.search(details["artist"], details["title"])
    failed = failed_offers(s.queue)
    rows = []
    for item in items:
        s.queue.offers[item["key"]] = item
        rows.append(offer_row(item, match.score(details, item), failed.get(item["key"])))
    rows.sort(key=lambda r: (bool(r["failed"]), ORDER[r["tier"]], -r["score"], -r["size"]))
    return jsonify(results=rows, problems=problems)


def has_words(name, words):
    return f" {words} " in f" {name} "


def artist_card(artist, held, first_of_name=True):
    """Deezer matches on name alone, so only the likeliest artist of a name may use its photo."""
    fields = ("id", "name", "kind", "country", "begin", "end", "ended", "note", "tags")
    return {**{k: artist.get(k) for k in fields}, "in_library": artist["id"] in held, "best": first_of_name}


def artists_named(s, text, refresh=False):
    """MusicBrainz artists whose name holds these words, kept so each name is only asked once."""
    words = norm(text)
    ids, saved_at = s.store.search("artist", words)
    found = [s.store.artist(i) for i in ids] if ids is not None and not refresh else None
    if found is None or None in found:
        found = [a for a in s.brainz.artists(text) if has_words(norm(a["name"]), words)]
        for artist in found:
            s.store.save_artist(artist)
        s.store.save_search("artist", words, [a["id"] for a in found])
        saved_at = time.time()
    return found, saved_at


@bp.get("/api/artists")
def search_artists():
    words = norm(request.args.get("q", ""))
    if not words:
        return jsonify(artists=[])
    s = svc()
    found, saved_at = artists_named(s, request.args["q"], "refresh" in request.args)
    held = s.owned.artists
    order = sorted(enumerate(found), key=lambda pair: (pair[1]["id"] not in held, norm(pair[1]["name"]) != words, pair[0]))
    named, artists = set(), []
    for _, artist in order[:ARTIST_LIMIT]:
        key = norm(artist["name"])
        artists.append(artist_card(artist, held, key not in named))
        named.add(key)
    return jsonify(artists=artists, saved_at=saved_at)


MATCH_TRIES = 5


def albums_known(s, artist):
    """An artist's albums from MusicBrainz, kept like the artist page keeps them."""
    if not (s.store.artist(artist["id"]) or {}).get("albums_at"):
        s.store.save_artist(artist)
        s.store.save_albums(artist["id"], s.brainz.artist_albums(artist["id"]))
    return s.store.albums_of(artist["id"])


@bp.get("/api/plex/artist/<int:rating>/match")
def match_plex_artist(rating):
    """The MusicBrainz artist for a Plex artist Plex hasn't matched itself. One artist of exactly that
    name is taken; with several, the one sharing the most albums with what's in Plex, if any does."""
    s = svc()
    row = s.store.plex_artist_by_rating(str(rating))
    if not row:
        raise Refusal(404)
    if row["mbid"]:
        return jsonify(id=row["mbid"])
    named = [a for a in artists_named(s, row["name"])[0] if norm(a["name"]) == norm(row["name"])]
    chosen = named[0] if len(named) == 1 else None
    if len(named) > 1:
        held = {album_key(a["title"]) for a in s.store.plex_albums() if a["artist_rating"] == str(rating)}
        shared = [(len(held & {album_key(b["title"]) for b in albums_known(s, a)}), a) for a in named[:MATCH_TRIES]]
        best = max(shared, key=lambda pair: pair[0], default=(0, None))
        chosen = best[1] if best[0] else None
    if not chosen:
        return jsonify(id=None)
    s.store.link_plex(str(rating), chosen["id"], row["name"])
    s.owned.load(s.store)
    return jsonify(id=chosen["id"])


@bp.get("/api/artist/<artist_id>")
def artist_page(artist_id):
    checked_id(artist_id, "artist")
    s = svc()
    store, brainz = s.store, s.brainz
    refresh = "refresh" in request.args
    artist = None if refresh else store.artist(artist_id)
    if not artist:
        store.save_artist(brainz.artist(artist_id))
        if refresh:
            store.clear_photo(artist_id)
            (PHOTOS / f"{artist_id}.jpg").unlink(missing_ok=True)
        artist = store.artist(artist_id)
    if refresh or not artist["albums_at"]:
        store.save_albums(artist_id, brainz.artist_albums(artist_id))
        artist = store.artist(artist_id)
    albums = store.albums_of(artist_id)
    albums.sort(key=lambda a: (a["year"], a["title"].casefold()), reverse=True)
    mark_owned(albums)
    listed = {album_key(a["title"]) for a in albums}
    others = []
    for row in store.plex_albums():
        if row["mbid"] == artist_id and album_key(row["title"]) not in listed:
            others.append({"title": row["title"], "year": row["year"], "cover": f"/api/plex/thumb/{row['rating']}",
                           "plex_url": s.plex.web_url(row["rating"])})
    s.filling.extend(a["id"] for a in sorted(albums, key=lambda a: a["kind"] != "Album")
                     if not store.has_details(a["id"]) and a["id"] not in s.filling)
    s.fill.set()
    in_plex = s.store.plex_artist(artist_id)
    card = {**artist_card(artist, s.owned.artists), "plex_url": s.plex.web_url(in_plex["rating"]) if in_plex else ""}
    return jsonify(artist=card, albums=albums, others=others, saved_at=artist["albums_at"])


def image(body_bytes, kind):
    return Response(body_bytes, content_type=kind, headers={"Cache-Control": "max-age=86400"})


def plex_image(thumb):
    try:
        return image(*svc().plex.image(thumb))
    except Exception as error:
        log.warning("Plex image failed: %s", type(error).__name__)
        raise Refusal(404) from None


@bp.get("/api/artist/<artist_id>/photo")
def artist_photo(artist_id):
    """Plex's photo where it has one, otherwise one fetched once and kept on disk."""
    checked_id(artist_id, "artist")
    s = svc()
    plex_row = s.store.plex_artist(artist_id)
    if plex_row and plex_row["thumb"]:
        return plex_image(plex_row["thumb"])
    path = PHOTOS / f"{artist_id}.jpg"
    if path.exists():
        response = send_file(path, mimetype="image/jpeg")
        response.headers["Cache-Control"] = "max-age=86400"
        return response
    artist = s.store.artist(artist_id)
    fresh = artist and artist["photo"] == "none" and time.time() - artist["photo_at"] < PHOTO_RETRY
    if not artist or fresh or request.args.get("best") == "0":
        raise Refusal(404)
    found = s.deezer.photo(artist["name"])
    s.store.set_photo(artist_id, "local" if found else "none")
    if not found:
        raise Refusal(404)
    path.write_bytes(found)
    return image(found, "image/jpeg")


@bp.get("/api/plex/thumb/<int:rating>")
def plex_thumb(rating):
    thumb = svc().store.plex_thumb(str(rating))
    if not thumb:
        raise Refusal(404)
    return plex_image(thumb)


@bp.get("/api/artist/<artist_id>/videos")
def artist_videos(artist_id):
    checked_id(artist_id, "artist")
    s = svc()
    artist = s.store.artist(artist_id)
    if not artist:
        raise Refusal(404)
    if not s.indexers.active():
        raise SourceError("Add your indexer API keys in Settings first")
    items, problems = s.indexers.videos(artist["name"])
    failed = failed_offers(s.queue)
    rows = []
    for item in items:
        s.queue.offers[item["key"]] = item
        rows.append(offer_row(item, match.score_video(artist["name"], item), failed.get(item["key"])))
    rows.sort(key=lambda r: (bool(r["failed"]), ORDER[r["tier"]], -r["score"], r["song"].casefold()))
    return jsonify(results=rows, problems=problems)


@bp.post("/api/videos")
def add_video():
    data, s = body(), svc()
    artist = s.store.artist(str(data.get("artist_id", "")))
    offer = s.queue.offers.get(str(data.get("release", "")))
    if not artist or not offer:
        raise Refusal(409, "That result has expired. Search for videos again.")
    if not s.sab.ready:
        raise Refusal(409, "Add your SABnzbd details in Settings first")
    if not folders_ready():
        raise Refusal(409, "Set your folders in Settings first")
    with s.queue.lock:
        if any(j["album_id"] == f"video-{offer['key']}" and j["state"] in ACTIVE for j in s.queue.jobs):
            raise Refusal(409, "Already queued")
    verdict = match.score_video(artist["name"], offer)
    song = str(data.get("song") or verdict["song"]).strip()
    if not song:
        raise Refusal(409, "Encore can't tell which song this is")
    return jsonify(public(s.queue.add_video(artist, offer, {**verdict, "song": song})))


def single_row(artist, video, wanted=""):
    """A YouTube video offered as one of the artist's music videos, with the song it seems to be."""
    scored = match.score_youtube({"title": wanted or match.youtube_song(artist["name"], video), "seconds": 0}, artist["name"], video)
    audio = match.audio_only(video["channel"])
    notes = [n for n in scored["notes"] if not n.startswith("Audio only")]
    if audio:
        notes.insert(0, "Audio over a still picture, not a real video")
    return {**video, "song": match.youtube_song(artist["name"], video), "audio_only": audio, "notes": notes,
            "score": scored["score"] - (80 if audio else 0)}


@bp.post("/api/artist/<artist_id>/youtube")
def artist_youtube(artist_id):
    """Finds a music video on YouTube from a pasted link or a song name, for songs MusicBrainz doesn't list yet."""
    checked_id(artist_id, "artist")
    s = svc()
    artist = s.store.artist(artist_id)
    if not artist:
        raise Refusal(404)
    text = str(body().get("q", "")).strip()[:200]
    if not text:
        raise Refusal(400, "Paste a YouTube link or type the song's name")
    # Only a link counts: a bare video id is 11 letters, and so is a song called Starburster.
    video_id = youtube_id(text) if re.search(r"[/.]", text) else None
    if video_id:
        return jsonify(results=[single_row(artist, s.youtube.lookup(video_id))])
    if re.match(r"^\w+://", text) or re.search(r"youtu\.?be", text, re.I):
        raise Refusal(400, "That isn't a YouTube video link")
    found = [v for v in s.youtube.search(f"{artist['name']} {text}") if not v["live"]]
    for video in found:
        s.store.save_youtube_video(video)
    rows = sorted((single_row(artist, v, text) for v in found), key=lambda r: -r["score"])
    return jsonify(results=rows[:5])


@bp.post("/api/videos/youtube")
def add_youtube_video():
    data, s = body(), svc()
    artist = s.store.artist(str(data.get("artist_id", "")))
    video_id = youtube_id(str(data.get("video", "")))
    cached = s.store.youtube_video(video_id) if video_id else None
    if not artist or not cached:
        raise Refusal(409, "That video has expired. Find it again.")
    if not folders_ready():
        raise Refusal(409, "Set your folders in Settings first")
    song = " ".join(str(data.get("song", "")).split())[:120]
    if not filing.safe(song):
        raise Refusal(400, "Give the song a name")
    want_video, want_audio = bool(data.get("video_file", True)), bool(data.get("audio"))
    if not (want_video or want_audio):
        raise Refusal(400, "Choose the video, the song or both")
    with s.queue.lock:
        if any(j["album_id"] == f"video-yt-{video_id}" and j["state"] in ACTIVE for j in s.queue.jobs):
            raise Refusal(409, "Already queued")
    return jsonify(public(s.queue.add_youtube_video(artist, cached["video"], song, want_video, want_audio)))


# ---------------------------------------------------------------- library and wanted

@bp.get("/api/library")
def library_page():
    store = svc().store
    artists = [{"id": a["mbid"], "name": a["name"], "albums": a["albums"], "rating": a["rating"]} for a in store.plex_artists()]
    return jsonify(artists=artists, albums=len(store.plex_albums()), saved_at=store.plex_at())


@bp.post("/api/library/sync")
def library_sync():
    try:
        sync_plex(svc())
    except Exception as error:
        log.warning("Plex sync failed: %s", type(error).__name__)
        raise SourceError("Plex isn't answering") from None
    return library_page()


@bp.get("/api/wanted")
def wanted_list():
    return jsonify(albums=svc().store.wanted())


@bp.post("/api/wanted")
def add_wanted():
    store = svc().store
    added = 0
    for album_id in body().get("album_ids") or []:
        album = store.album(str(album_id))
        if album and album["id"] not in store.wanted_ids():
            store.want(album)
            svc().publish("wanted.added", f"Wanted: {album['artist']} – {album['title']}", api_wanted_item(album))
            added += 1
    return jsonify(added=added)


def api_wanted_item(album):
    from .api import wanted_item
    return wanted_item(album)


@bp.delete("/api/wanted/<album>")
def remove_wanted(album):
    svc().store.unwant(album)
    return jsonify(ok=True)


# ---------------------------------------------------------------- status and settings

@bp.get("/healthz")
def healthz():
    """Homelab app standard: no login; busy lists jobs a restart would cut short (checking or filing)."""
    return {"ok": True, "busy": svc().queue.busy()}


@bp.get("/api/status")
def status():
    s = svc()
    state = "unset"
    if s.sab.ready:
        try:
            s.sab.version()
            state = "ok"
        except SourceError:
            state = "error"
    library = s.library.fresh()
    return jsonify(sab=state, indexers=len(s.indexers.active()), wanted=len(s.store.wanted_ids()),
                   plex=s.plex.status(), folders=folders_ready(),
                   free=shutil.disk_usage(engine.IMPORT if engine.IMPORT.is_dir() else DATA).free,
                   albums=sum(len(a) for a in library.artists.values()))


@bp.get("/api/settings")
def get_settings():
    s = svc()
    return jsonify({**s.settings.public(), "password": s.auth.enabled})


def visitor():
    """Behind a tunnel everyone arrives from the proxy, so wrong-password locks use the address
    it reports. Only the configured proxy is believed, because anyone can send that header."""
    if TRUSTED_PROXY and request.remote_addr == TRUSTED_PROXY:
        return request.headers.get("CF-Connecting-IP") or request.remote_addr
    return request.remote_addr


def over_https():
    return bool(TRUSTED_PROXY) and request.remote_addr == TRUSTED_PROXY and request.headers.get("X-Forwarded-Proto") == "https"


def remember(response):
    response.set_cookie(COOKIE, svc().auth.token(), max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="Lax", secure=over_https())


def safe_next(value):
    """Where to go after signing in: a path on this site only, never another site (//evil, /\\evil)."""
    value = str(value or "")
    return value if value.startswith("/") and not value.startswith(("//", "/\\")) else "/"


@bp.get("/login")
def login_page():
    auth = svc().auth
    if not auth.enabled or auth.valid(request.cookies.get(COOKIE)):
        return redirect(safe_next(request.args.get("next")))
    return render_template("login.html")


@bp.post("/api/login")
def log_in():
    auth = svc().auth
    address = visitor()
    if auth.locked(address):
        return jsonify(error="Too many tries. Wait a minute and try again."), 429
    if not auth.check(str(body().get("password", ""))):
        auth.failed(address)
        return jsonify(error="That isn't the password."), 401
    auth.passed(address)
    response = jsonify(ok=True)
    remember(response)
    return response


@bp.post("/api/logout")
def log_out():
    response = jsonify(ok=True)
    response.delete_cookie(COOKIE)
    return response


@bp.post("/api/password")
def set_password():
    """Blank new turns the password off. Changing it needs the current one, even when signed in."""
    auth = svc().auth
    address = visitor()
    if auth.locked(address):
        return jsonify(error="Too many tries. Wait a minute and try again."), 429
    data = body()
    new = str(data.get("new", ""))
    if auth.enabled and not auth.check(str(data.get("current", ""))):
        auth.failed(address)
        return jsonify(error="That isn't the current password."), 403
    if new and len(new) < MIN_LENGTH:
        return jsonify(error=f"Use at least {MIN_LENGTH} characters."), 400
    auth.passed(address)
    if new:
        auth.set(new)
    else:
        auth.clear()
    response = jsonify(password=auth.enabled)
    if new:
        remember(response)
    return response


@bp.post("/api/settings")
def save_settings():
    s = svc()
    s.settings.update(body())
    use_folders(s.settings)
    s.library.built = 0
    s.indexers.cache.clear()
    return jsonify(s.settings.public())


@bp.post("/api/settings/test")
def test_settings():
    """Reads capabilities only, which doesn't count as a search on most indexers' limits."""
    s = svc()
    results = []
    for indexer in s.indexers.active():
        try:
            status_code, answer = s.indexers.call(indexer, t="caps")
            good = status_code == 200 and b"<caps" in answer[:2000]
            results.append({"name": indexer["name"], "ok": good, "text": "Connected" if good else "Unexpected answer"})
        except SourceError as error:
            results.append({"name": indexer["name"], "ok": False, "text": str(error)})
    try:
        results.append({"name": "SABnzbd", "ok": True, "text": f"Connected, version {s.sab.version()}"})
    except SourceError as error:
        results.append({"name": "SABnzbd", "ok": False, "text": str(error)})
    for label, folder in (("Music folder", engine.MUSIC), ("Import folder", engine.IMPORT)):
        good = folder.is_dir() and engine.can_write(folder)
        results.append({"name": label, "ok": good, "text": "Ready" if good else "Encore can't write here"})
    if engine.DOWNLOADS == engine.NOWHERE:
        results.append({"name": "Downloads folder", "ok": False, "text": "Not set"})
    if s.plex.server:
        state = s.plex.status()
        text = {"ok": "Connected", "none": "Press Connect Plex at the top of the page"}.get(state, "Can't reach it")
        results.append({"name": "Plex", "ok": state == "ok", "text": text})
    return jsonify(results=results)


# ---------------------------------------------------------------- the queue

@bp.get("/api/queue")
def list_queue():
    queue = svc().queue
    with queue.lock:
        return jsonify(jobs=[public(j) for j in queue.jobs])


@bp.post("/api/queue")
def add_job():
    data, s = body(), svc()
    album_id, key = str(data.get("album_id", "")), str(data.get("release", ""))
    checked_id(album_id)
    with s.queue.lock:
        if any(j["album_id"] == album_id and j["state"] in ACTIVE for j in s.queue.jobs):
            raise Refusal(409, "Already queued")
    offer = s.queue.offers.get(key)
    if not offer:
        raise Refusal(409, "That result has expired. Open the album again.")
    if not s.sab.ready:
        raise Refusal(409, "Add your SABnzbd details in Settings first")
    if not folders_ready():
        raise Refusal(409, "Set your folders in Settings first")
    album = s.brainz.album(album_id)
    chose_other = "Not FLAC" in match.score(album, offer)["notes"]
    return jsonify(public(s.queue.add(album, offer, bool(data.get("fix_artist")), chose_other)))


# ---------------------------------------------------------------- YouTube albums

def editions_of(details):
    """Editions with different track lists, main one first. Many releases share a track list, so
    only the earliest of each is kept, or a big album would offer hundreds of the same choice."""
    seen, found = set(), []
    main = details["tracks_list"]
    ordered = sorted(details["editions"], key=lambda e: (e["tracks"] != main, e["date"] or "9999"))
    for edition in ordered:
        shape = tuple((t["disc"], t["number"], norm(t["title"])) for t in edition["tracks"])
        if shape in seen:
            continue
        seen.add(shape)
        discs = len({t["disc"] for t in edition["tracks"]})
        parts = [f"{len(edition['tracks'])} tracks", f"{discs} discs" if discs > 1 else "",
                 (edition["date"] or "")[:4], edition["title"] if norm(edition["title"]) != norm(details["title"]) else "",
                 edition["label"]]
        found.append({**edition, "label_text": " · ".join(p for p in parts if p)})
    return found


def track_key(track):
    return f"{track['disc']}-{track['number']}"


def youtube_row(s, details, track, video_id):
    cached = s.store.youtube_video(video_id) if video_id else None
    video = cached["video"] if cached else None
    if video:
        video = {**video, **match.score_youtube(track, details["artist"], video),
                 "audio_only": match.audio_only(video["channel"])}
    return {**track, "key": track_key(track), "video": video}


def builder(s, details):
    draft = s.store.youtube_draft(details["id"])
    editions = editions_of(details)
    edition = next((e for e in editions if e["id"] == draft["edition"]), editions[0])
    rows = [youtube_row(s, details, t, draft["links"].get(track_key(t))) for t in edition["tracks"]]
    return {"edition": edition["id"], "editions": [{"id": e["id"], "label": e["label_text"]} for e in editions],
            "discs": len({t["disc"] for t in edition["tracks"]}), "tracks": rows,
            "seconds": sum(t.get("seconds") or 0 for t in edition["tracks"]),
            "whole": whole_row(s, details, edition, draft["links"].get(WHOLE))}


# The whole-album video is kept in the same draft as the per-track links, under a key no track has.
WHOLE = "whole"


def whole_score(details, edition, video):
    """A video of the whole album, scored like a track whose length is the album's and whose name is its title."""
    album = {"title": details["title"], "seconds": sum(t.get("seconds") or 0 for t in edition["tracks"])}
    return {**video, **match.score_youtube(album, details["artist"], video, whole=True), "audio_only": match.audio_only(video["channel"])}


def whole_row(s, details, edition, video_id):
    cached = s.store.youtube_video(video_id) if video_id else None
    return whole_score(details, edition, cached["video"]) if cached else None


def draft_edition(s, details):
    editions = editions_of(details)
    draft = s.store.youtube_draft(details["id"])
    return draft, next((e for e in editions if e["id"] == draft["edition"]), editions[0])


def youtube_track(s, album, key):
    details = s.brainz.album(checked_id(album))
    built = builder(s, details)
    row = next((r for r in built["tracks"] if r["key"] == key), None)
    if not row:
        raise Refusal(404, "That track isn't on this edition")
    return details, row


def gaps_job(album, job_id):
    """A Usenet job that's waiting with tracks missing, whose gaps can be filled from YouTube."""
    job = find_job(job_id)
    if job["album_id"] != album or job.get("kind") or not job.get("missing") or job["state"] != "failed" or not job.get("kept"):
        raise Refusal(409, "That download has no missing tracks waiting to be filled")
    return job


@bp.get("/api/album/<album>/youtube")
def youtube_builder(album):
    """With ?job=, only the tracks that job's download is missing, in the edition it matched."""
    s = svc()
    details = s.brainz.album(checked_id(album))
    job_id = request.args.get("job")
    if not job_id:
        return jsonify(builder(s, details))
    job = gaps_job(album, job_id)
    draft = s.store.youtube_draft(album)
    if draft["edition"] != job["edition_id"]:
        s.store.save_youtube_draft(album, job["edition_id"], draft["links"])
    built = builder(s, details)
    keys = {m["key"] for m in job["missing"]}
    built["fill"] = {"job": job["id"], "have": len(built["tracks"]) - len(keys)}
    built["tracks"] = [t for t in built["tracks"] if t["key"] in keys]
    return jsonify(built)


@bp.post("/api/album/<album>/youtube/edition")
def youtube_edition(album):
    s = svc()
    details = s.brainz.album(checked_id(album))
    edition = str(body().get("edition", ""))
    if edition not in {e["id"] for e in details["editions"]}:
        raise Refusal(400, "That isn't an edition of this album")
    s.store.save_youtube_draft(album, edition, s.store.youtube_draft(album)["links"])
    return jsonify(builder(s, details))


@bp.post("/api/album/<album>/youtube/link")
def youtube_link(album):
    """Sets one track's video from a pasted link (or clears it when blank), and saves the draft."""
    s, data = svc(), body()
    details, row = youtube_track(s, album, str(data.get("key", "")))
    text = str(data.get("url", "")).strip()
    video_id = youtube_id(text) if text else None
    if text and not video_id:
        raise Refusal(400, "That isn't a YouTube video link")
    if video_id:
        s.youtube.lookup(video_id)
    draft = s.store.youtube_draft(album)
    links = {**draft["links"], row["key"]: video_id} if video_id else {k: v for k, v in draft["links"].items() if k != row["key"]}
    s.store.save_youtube_draft(album, draft["edition"] or builder(s, details)["edition"], links)
    return jsonify(youtube_row(s, details, row, video_id))


@bp.post("/api/album/<album>/youtube/find")
def youtube_find(album):
    s = svc()
    details, row = youtube_track(s, album, str(body().get("key", "")))
    found = s.youtube.search(f"{row['artist'] or details['artist']} {row['title']}")
    for video in found:
        s.store.save_youtube_video(video)
    rows = [{**v, **match.score_youtube(row, details["artist"], v), "audio_only": match.audio_only(v["channel"])} for v in found]
    rows.sort(key=lambda v: -v["score"])
    return jsonify(results=rows[:5])


@bp.post("/api/album/<album>/youtube/whole")
def youtube_whole(album):
    """Sets (or clears, when blank) the one video that has the whole album, and saves it with the draft.
    Always looked up fresh, for its chapters: a search result doesn't carry them."""
    s, data = svc(), body()
    details = s.brainz.album(checked_id(album))
    text = str(data.get("url", "")).strip()
    video_id = youtube_id(text) if text else None
    if text and not video_id:
        raise Refusal(400, "That isn't a YouTube video link")
    if video_id:
        s.youtube.lookup(video_id, fresh=True)
    draft, edition = draft_edition(s, details)
    links = {**draft["links"], WHOLE: video_id} if video_id else {k: v for k, v in draft["links"].items() if k != WHOLE}
    s.store.save_youtube_draft(album, edition["id"], links)
    return jsonify(builder(s, details))


@bp.post("/api/album/<album>/youtube/whole/find")
def youtube_whole_find(album):
    s = svc()
    details = s.brainz.album(checked_id(album))
    _, edition = draft_edition(s, details)
    found = s.youtube.search(f"{details['artist']} {details['title']} full album")
    for video in found:
        s.store.save_youtube_video(video)
    rows = sorted((whole_score(details, edition, v) for v in found if not v["live"]), key=lambda v: -v["score"])
    return jsonify(results=rows[:5])


@bp.post("/api/queue/youtube/whole")
def add_youtube_album_job():
    data, s = body(), svc()
    album_id = checked_id(str(data.get("album_id", "")))
    if not folders_ready():
        raise Refusal(409, "Set your folders in Settings first")
    with s.queue.lock:
        if any(j["album_id"] == album_id and j["state"] in ACTIVE for j in s.queue.jobs):
            raise Refusal(409, "Already queued")
    details = s.brainz.album(album_id)
    draft, edition = draft_edition(s, details)
    video_id = draft["links"].get(WHOLE)
    if not video_id:
        raise Refusal(409, "Add the link to the album's video first")
    if len(edition["tracks"]) < 2:
        raise Refusal(409, "This edition has only one track, so there's nothing to split")
    # Fresh, for its timestamps: a search since may have cached it without them.
    return jsonify(public(s.queue.add_youtube_album(details, edition, s.youtube.lookup(video_id, fresh=True))))


# ---------------------------------------------------------------- an album MusicBrainz doesn't have, from one video

ALBUM_NOISE = re.compile(r"[(\[]?\b(full album|complete album|full lp|album completo|whole album|full ep)\b[)\]]?", re.I)


def album_guess(video):
    """What a person would probably type for a whole-album upload: the album from the title (a quoted
    name, or "Artist - Album"), the artist from the title or else the channel, the year the description
    gives (an old album uploaded later), and the track names from its track list or chapters."""
    title = video["title"]
    quoted = re.search(r"[\"“”']([^\"“”']{2,80})[\"“”']", title)
    parts = [p.strip() for p in re.split(r"\s+[-–—|]\s+|~|\|", ALBUM_NOISE.sub(" ", title)) if p.strip(" *~-")]
    channel = re.sub(r"\s*-\s*Topic$|VEVO$|\s+Official$", "", video.get("channel") or "", flags=re.I).strip()
    artist, album = channel, ""
    if quoted:
        album = quoted.group(1).strip()
    elif len(parts) >= 2:
        artist, album = parts[0], parts[1]
    elif parts:
        album = parts[0]
    album = " ".join(ALBUM_NOISE.sub(" ", album).strip(" -–|~*\"").split())
    named = [c["title"] for c in video.get("chapters") or [] if c["title"]]
    tracks = video.get("tracklist") or (named if len(named) >= 2 and len(named) == len(video.get("chapters") or []) else [])
    return {"artist": artist, "title": album, "year": video.get("recorded") or "", "tracks": tracks}


def playlist_guess(playlist, first):
    """For a playlist that's an album: YouTube Music's own album playlists name the album, and their
    first video names the artist and the year it came out. Otherwise the playlist's name, the channel
    most of its videos come from, and each video's song from its title."""
    entries = playlist["entries"]
    channels = Counter(re.sub(r"\s*-\s*Topic$|VEVO$", "", e["channel"], flags=re.I).strip() for e in entries if e["channel"])
    artist = (first or {}).get("artist", "").split(",")[0].strip() or (channels.most_common(1)[0][0] if channels else playlist["channel"])
    title = (first or {}).get("album") or re.sub(r"^album\s*[-–:]\s*", "", playlist["title"], flags=re.I)
    title = " ".join(ALBUM_NOISE.sub(" ", title).split())
    year = (first or {}).get("released") or (first or {}).get("recorded") or ""
    names = [e["title"] if match.audio_only(e["channel"]) else (match.youtube_song(artist, e) or e["title"]) for e in entries]
    return {"artist": artist, "title": title, "year": year, "tracks": names}


def playlist_lookup(s, list_id):
    playlist = s.youtube.playlist(list_id)
    if not playlist["entries"]:
        raise Refusal(409, "That playlist has no videos Encore can use")
    if len(playlist["entries"]) > 99:
        raise Refusal(409, "That playlist has more than 99 videos, which is more than an album")
    for video in playlist["entries"]:
        s.store.save_youtube_video(video)
    try:
        first = s.youtube.lookup(playlist["entries"][0]["id"], fresh=True)
    except SourceError:
        first = None
    job = next((j for j in s.queue.jobs if j["album_id"] == f"yt-list-{list_id}" and j["state"] in ACTIVE), None)
    return jsonify(playlist=playlist, guess=playlist_guess(playlist, first), job=public(job) if job else None)


@bp.post("/api/youtube/album")
def youtube_free_lookup():
    """Looks up one video, or a playlist, of an album MusicBrainz doesn't have, with Encore's guess at its details."""
    s = svc()
    text = str(body().get("url", "")).strip()
    list_id = playlist_id(text)
    if list_id:
        return playlist_lookup(s, list_id)
    video_id = youtube_id(text)
    if not video_id:
        raise Refusal(400, "Paste a YouTube video or playlist link")
    video = s.youtube.lookup(video_id, fresh=True)
    if video["live"]:
        raise Refusal(409, "That's a live stream, not an upload")
    job = next((j for j in s.queue.jobs if j["album_id"] == f"yt-album-{video_id}" and j["state"] in ACTIVE), None)
    return jsonify(video=video, guess=album_guess(video), job=public(job) if job else None)


def typed_album(data):
    artist, title = (" ".join(str(data.get(k, "")).split())[:200] for k in ("artist", "title"))
    year = str(data.get("year", "")).strip()
    if not artist or not title:
        raise Refusal(400, "Type the artist and the album's name")
    if year and not re.fullmatch(r"(19|20)\d\d", year):
        raise Refusal(400, "The year should be four digits, like 2001, or blank")
    if not folders_ready():
        raise Refusal(409, "Set your folders in Settings first")
    return artist, title, year


def add_youtube_list_job(s, data, list_id):
    """Each video of the playlist that's kept is one track, in the order sent, with the name typed for it."""
    rows = data.get("tracks")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 99:
        raise Refusal(400, "Keep at least one track, and no more than 99")
    tracks, seen = [], set()
    for place, row in enumerate(rows):
        video_id = youtube_id(str((row or {}).get("video", ""))) if isinstance(row, dict) else None
        cached = s.store.youtube_video(video_id) if video_id else None
        if not cached or video_id in seen:
            raise Refusal(409, "Look the playlist up again")
        seen.add(video_id)
        video = cached["video"]
        name = " ".join(str(row.get("title", "")).split())[:200] or f"Track {place + 1}"
        tracks.append({"video": video_id, "title": name, "seconds": video["seconds"], "channel": video["channel"]})
    artist, title, year = typed_album(data)
    with s.queue.lock:
        if any(j["album_id"] == f"yt-list-{list_id}" and j["state"] in ACTIVE for j in s.queue.jobs):
            raise Refusal(409, "Already queued")
    return jsonify(public(s.queue.add_youtube_list(list_id, artist, title, year, tracks)))


@bp.post("/api/queue/youtube/free")
def add_youtube_free_job():
    data, s = body(), svc()
    if data.get("playlist"):
        list_id = playlist_id(str(data["playlist"]))
        if not list_id:
            raise Refusal(400, "That isn't a YouTube playlist")
        return add_youtube_list_job(s, data, list_id)
    video_id = youtube_id(str(data.get("video", "")))
    cached = s.store.youtube_video(video_id) if video_id else None
    if not cached:
        raise Refusal(409, "Look the video up first")
    artist, title, year = typed_album(data)
    names = data.get("tracks") or []
    if not isinstance(names, list) or len(names) > 99:
        raise Refusal(400, "Up to 99 tracks, one name each")
    names = [" ".join(str(n).split())[:200] for n in names if str(n).strip()]
    if len(names) == 1:
        raise Refusal(400, "One track is nothing to split. List them all, or leave the list empty")
    with s.queue.lock:
        if any(j["album_id"] == f"yt-album-{video_id}" and j["state"] in ACTIVE for j in s.queue.jobs):
            raise Refusal(409, "Already queued")
    return jsonify(public(s.queue.add_youtube_free(cached["video"], artist, title, year, names)))


def review_job(job_id):
    """A whole-album job with its plan saved: waiting to be checked, or failed after it was."""
    job = find_job(job_id)
    plan, path = svc().queue.split_plan(job)
    if not plan or job["state"] not in ("review", "failed"):
        raise Refusal(409, "That album isn't waiting to be checked")
    return job, plan, path


@bp.get("/api/queue/<job_id>/split")
def split_view(job_id):
    job, plan, _ = review_job(job_id)
    chapters = job["source"].get("chapters") or []
    free = bool(job["source"].get("free"))
    if free:
        tracks = [{"key": f"1-{n + 1}", "disc": 1, "number": n + 1, "title": t, "seconds": 0, "chapter": ""}
                  for n, t in enumerate(plan["titles"])]
    else:
        named = plan.get("source") == "chapters"
        tracks = [{**t, "chapter": chapters[i]["title"] if named and i < len(chapters) else ""}
                  for i, t in enumerate(job["source"]["tracks"])]
    return jsonify(job=public(job), tracks=tracks, discs=len({t["disc"] for t in tracks}), free=free,
                   marks=split.aligned_marks(plan, plan["cuts"]), found_marks=plan.get("marks"),
                   titles=plan.get("titles"), found_titles=plan.get("found_titles"),
                   **{k: plan.get(k) for k in ("duration", "cuts", "found", "source", "warnings", "gaps", "peaks")})


@bp.post("/api/queue/<job_id>/split")
def split_save(job_id):
    """Saves where each track starts (so a restart keeps them), and with file: true queues the album to be cut and filed."""
    queue, data = svc().queue, body()
    job, plan, path = review_job(job_id)
    free = job["source"].get("free")
    try:
        cuts = split.checked_cuts(data.get("cuts"), None if free else len(job["source"]["tracks"]), plan["duration"])
        if free:
            plan["titles"] = split.checked_titles(data.get("titles"), len(cuts) - 1)
    except split.SplitError as error:
        raise Refusal(400, str(error)) from None
    plan["cuts"] = cuts
    split.save(path, plan)
    if data.get("file"):
        with queue.lock:
            if job["state"] not in ("review", "failed"):
                raise Refusal(409, "That album isn't waiting to be checked")
            queue.jobs.remove(job)
            queue.jobs.append(job)
            job["confirmed"] = True
            queue.update(job, state="queued", message="", percent=0)
        queue.wake.set()
    return jsonify(job=public(job), cuts=cuts, titles=plan.get("titles"))


@bp.get("/api/queue/<job_id>/split/audio")
def split_audio(job_id):
    """The album's sound, for listening to each join. Ranged, so the page can jump about in it."""
    job, _, path = review_job(job_id)
    sound = path.parent / "album.m4a"
    if not sound.exists():
        raise Refusal(404)
    return send_file(sound, mimetype="audio/mp4", conditional=True, max_age=0)


def linked_tracks(s, details, partial):
    built = builder(s, details)
    linked = [r for r in built["tracks"] if r["video"]]
    if not linked:
        raise Refusal(409, "Add a link for at least one track first")
    if len(linked) < len(built["tracks"]) and not partial:
        raise Refusal(409, "Some tracks have no link yet")
    tracks = [{"key": r["key"], "disc": r["disc"], "number": r["number"], "title": r["title"], "video": r["video"]["id"],
               "seconds": r["video"]["seconds"], "channel": r["video"]["channel"]} for r in linked]
    return built["edition"], tracks


@bp.post("/api/queue/youtube")
def add_youtube_job():
    data, s = body(), svc()
    album_id = checked_id(str(data.get("album_id", "")))
    if not folders_ready():
        raise Refusal(409, "Set your folders in Settings first")
    with s.queue.lock:
        if any(j["album_id"] == album_id and j["state"] in ACTIVE for j in s.queue.jobs):
            raise Refusal(409, "Already queued")
    details = s.brainz.album(album_id)
    edition, tracks = linked_tracks(s, details, bool(data.get("partial")))
    return jsonify(public(s.queue.add_youtube(details, edition, tracks, len(tracks) < len(builder(s, details)["tracks"]))))


@bp.get("/api/youtube")
def youtube_status():
    return jsonify(version=svc().youtube.version())


@bp.post("/api/youtube/update")
def youtube_update():
    return jsonify(version=svc().youtube.update())


def find_job(job_id):
    job = svc().queue.get(job_id)
    if not job:
        raise Refusal(404)
    return job


def retry(job_id, partial=False, keep_format=False):
    queue = svc().queue
    job = find_job(job_id)
    if job["state"] != "failed":
        raise Refusal(409, "Only failed albums can be retried")
    if job.get("kind") == "youtube":
        # Picks up any links changed since it failed. Videos already downloaded stay for the next try.
        s = svc()
        details = s.brainz.album(job["album_id"])
        edition, tracks = linked_tracks(s, details, True)
        with queue.lock:
            job["source"] = {"type": "youtube", "edition": edition, "tracks": tracks}
            job["tracks"] = len(tracks)
    elif job.get("kind") in ("youtube_album", "youtube_list"):
        pass                                  # the downloads stay: a retry doesn't fetch them again
    elif keep_format:
        job["any_format"] = True
    elif not partial:
        queue.forget(job)
    reuse = bool((partial or keep_format) and job.get("kept"))
    with queue.lock:
        queue.jobs.remove(job)
        queue.jobs.append(job)
        queue.update(job, state="queued", message="", percent=0, partial=partial, reuse=reuse)
    queue.wake.set()
    return jsonify(public(job))


@bp.post("/api/queue/<job_id>/retry")
def retry_job(job_id):
    return retry(job_id)


@bp.post("/api/queue/<job_id>/partial")
def partial_job(job_id):
    find_job(job_id).pop("fill", None)
    return retry(job_id, partial=True)


@bp.post("/api/queue/<job_id>/fill")
def fill_job(job_id):
    """Runs the job again on the download it kept, with its missing tracks fetched from YouTube."""
    s = svc()
    job = find_job(job_id)
    gaps_job(job["album_id"], job_id)
    draft = s.store.youtube_draft(job["album_id"])
    if draft["edition"] != job["edition_id"]:
        raise Refusal(409, "Open Fill from YouTube on this download again")
    tracks = []
    for gap in job["missing"]:
        cached = s.store.youtube_video(draft["links"].get(gap["key"]) or "")
        if cached:
            video = cached["video"]
            tracks.append({**{k: gap[k] for k in ("key", "disc", "number", "title")}, "video": video["id"],
                           "seconds": video["seconds"], "channel": video["channel"]})
    if not tracks:
        raise Refusal(409, "Add a link for at least one missing track first")
    with s.queue.lock:
        job["fill"] = tracks
    return retry(job_id, partial=True)


@bp.post("/api/queue/<job_id>/import")
def import_job(job_id):
    return retry(job_id, keep_format=True)


@bp.post("/api/queue/order")
def order_queue():
    """Reorders only what's waiting. Anything already running keeps its place, whatever the page sent."""
    queue = svc().queue
    rank = {str(job_id): place for place, job_id in enumerate(body().get("ids") or [])}
    with queue.lock:
        waiting = [j for j in queue.jobs if j["state"] == "queued"]
        arranged = iter(sorted(waiting, key=lambda j: rank.get(j["id"], len(rank))))
        queue.jobs = [next(arranged) if j["state"] == "queued" else j for j in queue.jobs]
        queue.update()
    return jsonify(ok=True)


@bp.delete("/api/queue/<job_id>")
def remove_job(job_id):
    queue = svc().queue
    job = find_job(job_id)
    if queue.running is job:
        with queue.lock:
            job["cancel"] = True
    else:
        queue.forget(job)
        with queue.lock:
            if job in queue.jobs:
                queue.jobs.remove(job)
            queue.update()
    return jsonify(ok=True)


@bp.post("/api/queue/clear")
def clear_finished():
    queue = svc().queue
    with queue.lock:
        queue.jobs = [j for j in queue.jobs if j["state"] != "done"]
        queue.update()
    return jsonify(ok=True)


@bp.post("/api/plex/link")
def plex_link():
    return jsonify(svc().plex.start_link())


@bp.get("/api/plex/link/<int:pin>")
def plex_check(pin):
    return jsonify(linked=svc().plex.check_link(pin))


def page(template, **context):
    """With a password on, the guard has already checked the session, so anyone here is signed in."""
    return render_template(template, signed_in=svc().auth.enabled, **context)


@bp.get("/")
def index():
    return page("index.html", view="find")


@bp.get("/library")
def library_view():
    return page("index.html", view="library")


@bp.get("/wanted")
def wanted_view():
    return page("index.html", view="wanted")


@bp.get("/queue")
def queue_view():
    return page("index.html", view="queue")


@bp.get("/artist/<artist_id>")
def artist_view(artist_id):
    return page("index.html", view="artist", artist_id=checked_id(artist_id, "artist"))


@bp.get("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="favicon.ico"), 301)


@bp.get("/static/icon.svg")
def old_icon():
    """The icon's old address, before it moved to static/img/ (PWA-ICONS.md 1). The guard lets /static/ through."""
    return redirect(url_for("static", filename="img/icon.svg"), 301)


@bp.get("/settings")
def settings_view():
    from . import api
    return page("settings.html", view="settings", **api.settings_context())


def disk_size(path):
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


def size_text(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024


@bp.get("/about")
def about():
    """Design Uniformity 7.15: the app, Your data, Built with."""
    from importlib.metadata import version as pkg_version
    s = svc()
    db = s.store.db
    count = lambda table: f"{db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:,}"  # noqa: E731
    try:
        server = "gunicorn " + pkg_version("gunicorn")
    except Exception:  # noqa: BLE001  (python -m encore runs Flask's own server)
        server = "Flask's development server"
    plex = {"ok": "Connected", "error": "Can't reach it", "unset": "Not set up"}.get(s.plex.status(), "Not connected")
    database = disk_size(DATA / "encore.db") + disk_size(DATA / "encore.db-wal")
    info = {
        "tagline": "Finds albums on Usenet, checks every track, and files them where Plex can see them",
        "github": "https://github.com/marctew/Encore",
        "name": ("", "Encore"), "icon": url_for("static", filename="img/icon.svg"),
        "facts": [("Server", server), ("Plex", plex), ("Indexers", str(len(s.indexers.active())))],
        "data": [("Artists in Plex", count("plex_artists")), ("Albums in Plex", count("plex_albums")),
                 ("Artists looked up", count("artists")), ("Albums looked up", count("albums")),
                 ("Wanted", count("wanted")), ("Jobs in the queue", f"{len(s.queue.jobs):,}"),
                 ("Database", size_text(database)), ("Artist photos", size_text(disk_size(PHOTOS)))],
        "data_note": Markup("Everything lives in <code>{}</code>: the database, settings, the queue, "
                            "the Plex sign-in and artist photos.").format(DATA),
        "built_with": ["Flask, SQLite and gunicorn",
                       "MusicBrainz for artists, albums and track lists",
                       "Newznab indexers and SABnzbd for downloads, and yt-dlp for YouTube",
                       "mutagen for checking and tagging files",
                       "Plex for what you own, and Deezer for artist photos",
                       "Design Uniformity for the look"],
        "footnote": "Not affiliated with Plex, MusicBrainz, Deezer, SABnzbd or YouTube.",
    }
    return page("about.html", view="about", about=info)


@bp.post("/logout")
def log_out_form():
    response = redirect("/login" if svc().auth.enabled else "/")
    response.delete_cookie(COOKIE)
    return response


# ---------------------------------------------------------------- the app

def guard():
    """Every change needs the page's own header, so another site can't post here, and with a
    password on, everything but the sign-in and /healthz needs a valid session. /api/v1/ and
    /display/ are the outside API's: du_api checks their tokens (and a session's CSRF) instead."""
    from .api import csrf_value
    if request.path.startswith(("/api/v1/", "/display/")):
        return None
    # The shared Design system card sends X-CSRF-Token, and the shared API tokens and Webhooks cards
    # are plain forms with a csrf_token field: both carry csrf_token(), which is tied to the session.
    page_sent = request.headers.get("X-Encore") == "1" or request.headers.get("X-CSRF-Token") == csrf_value()
    form_sent = request.path.startswith("/settings/") and request.form.get("csrf_token") == csrf_value()
    if request.method not in ("GET", "HEAD") and not (page_sent or form_sent) and request.path not in FORM_POSTS:
        raise Refusal(403, "Missing request header")
    auth = svc().auth
    open_path = request.path in OPEN_PATHS or request.path.startswith("/static/")
    if auth.enabled and not open_path and not auth.valid(request.cookies.get(COOKIE)):
        if request.path.startswith("/api/"):
            return jsonify(error="Log in again."), 401
        wanted = request.full_path.rstrip("?")
        return redirect("/login" if wanted == "/" else f"/login?next={quote(wanted, safe='')}")
    return None


def harden(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Design Uniformity APP-STANDARD 12: static files are kept but revalidated (Flask answers 304 from the
    # ETag), so a deploy's CSS and JS show at once; pages are never kept. Anything else that sets its own
    # caching (photos and cover art, the display pages, the event stream) keeps it.
    if request.endpoint == "static":
        response.headers["Cache-Control"] = "no-cache"
        return response
    # Everything else is the signed-in person's own: pages and JSON are never kept anywhere, and what a
    # browser may keep (photos, Plex thumbnails) is private, so a shared cache in front of Encore (the
    # Cloudflare tunnel) never stores it and hands it to someone else. The outside API's own answers say
    # private or public themselves (du_api).
    cache = response.headers.get("Cache-Control")
    if not cache:
        response.headers["Cache-Control"] = "no-store"
    elif not any(word in cache for word in ("private", "public", "no-store")):
        response.headers["Cache-Control"] = f"private, {cache}"
    return response


def create_app():
    DATA.mkdir(parents=True, exist_ok=True)
    PHOTOS.mkdir(exist_ok=True)
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    web = Web()
    settings = Settings(DATA / "settings.json")
    use_folders(settings)
    store = Store(DATA / "encore.db")
    plex = Plex(DATA / "plex.json", settings)
    s = SimpleNamespace(web=web, settings=settings, store=store, plex=plex, auth=Auth(DATA / "auth.json"),
                        owned=Owned(), filling=deque(), fill=threading.Event(), deezer=Deezer(web),
                        brainz=MusicBrainz(web, store), indexers=Indexers(settings, web), youtube=YouTube(store),
                        sab=Sabnzbd(settings, web), library=Library(), started=False)

    def filed():
        try:
            sync_plex(s)
        except Exception as error:
            log.warning("Plex sync failed: %s", type(error).__name__)

    s.queue = Queue(s.indexers, s.sab, s.brainz, s.library, s.plex, s.store, filed, s.youtube)
    s.owned.load(store)
    app.extensions["encore"] = s
    app.register_blueprint(bp)
    try:
        from .du_flask import init_design      # copied in by the design updater, so it may not be here yet
    except ImportError:
        init_design = None
    if init_design:
        init_design(app, DATA / "design", version=__version__)
    from . import api
    api.install(app, s)                    # the outside API (/api/v1/, /display/) and the real csrf_token()
    app.before_request(guard)
    app.after_request(harden)

    @app.errorhandler(Refusal)
    def refused(error):
        return jsonify(error=error.text or HTTP_STATUS_CODES.get(error.status, "Refused")), error.status

    # 503, not 502: Cloudflare swaps an origin's 502 for its own error page, which would hide the message.
    @app.errorhandler(SourceError)
    def source_failed(error):
        return jsonify(error=str(error)), 503

    return app


def start_workers(app):
    """The queue, the hourly Plex sync and the track-list filler. Once per process."""
    s = app.extensions["encore"]
    if s.started:
        return
    s.started = True
    s.queue.start()
    from . import api
    api.start(app)
    threading.Thread(target=plex_rhythm, args=(s,), name="encore-plex", daemon=True).start()
    threading.Thread(target=fill_albums, args=(s,), name="encore-filler", daemon=True).start()
