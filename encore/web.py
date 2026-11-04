"""Encore's web side: the Flask app, its sign-in, and the JSON API the page calls.

create_app() builds a fresh app with its own services (the tests make several); start_workers()
starts the queue and the background jobs, once, in the process that serves requests.
"""
import logging
import re
import shutil
import threading
import time
from collections import deque
from types import SimpleNamespace

from flask import Blueprint, Flask, Response, current_app, jsonify, redirect, request, send_file

from . import engine, match
from .auth import MIN_LENGTH, SESSION_DAYS, Auth
from .config import DATA, PHOTOS, TRUSTED_PROXY
from .engine import ACTIVE, Library, Owned, Queue, album_key, folders_ready, public, use_folders
from .filing import norm
from .plex import Plex
from .sources import Deezer, Indexers, MusicBrainz, Sabnzbd, Settings, SourceError, Web
from .store import Store

PLEX_SYNC = 3600
PHOTO_RETRY = 30 * 86400
ARTIST_LIMIT = 12
COOKIE = "encore_session"
OPEN_PATHS = ("/login", "/api/login", "/healthz")
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
        held = None if album["owned"] else owned.find(album)
        if held:
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


@bp.get("/api/artists")
def search_artists():
    words = norm(request.args.get("q", ""))
    if not words:
        return jsonify(artists=[])
    s = svc()
    ids, saved_at = s.store.search("artist", words)
    found = [s.store.artist(i) for i in ids] if ids is not None and "refresh" not in request.args else None
    if found is None or None in found:
        found = [a for a in s.brainz.artists(request.args["q"]) if has_words(norm(a["name"]), words)]
        for artist in found:
            s.store.save_artist(artist)
        s.store.save_search("artist", words, [a["id"] for a in found])
        saved_at = time.time()
    held = s.owned.artists
    order = sorted(enumerate(found), key=lambda pair: (pair[1]["id"] not in held, norm(pair[1]["name"]) != words, pair[0]))
    named, artists = set(), []
    for _, artist in order[:ARTIST_LIMIT]:
        key = norm(artist["name"])
        artists.append(artist_card(artist, held, key not in named))
        named.add(key)
    return jsonify(artists=artists, saved_at=saved_at)


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
            others.append({"title": row["title"], "year": row["year"], "cover": f"/api/plex/thumb/{row['rating']}"})
    s.filling.extend(a["id"] for a in sorted(albums, key=lambda a: a["kind"] != "Album")
                     if not store.has_details(a["id"]) and a["id"] not in s.filling)
    s.fill.set()
    return jsonify(artist=artist_card(artist, s.owned.artists), albums=albums, others=others, saved_at=artist["albums_at"])


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
        if album:
            store.want(album)
            added += 1
    return jsonify(added=added)


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


@bp.get("/login")
def login_page():
    auth = svc().auth
    if not auth.enabled or auth.valid(request.cookies.get(COOKIE)):
        return redirect("/")
    return send_file(current_app.static_folder + "/login.html")


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
    if keep_format:
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


@bp.get("/")
def index():
    return send_file(current_app.static_folder + "/index.html")


# ---------------------------------------------------------------- the app

def guard():
    """Every change needs the page's own header, so another site can't post here, and with a
    password on, everything but the sign-in and /healthz needs a valid session."""
    if request.method not in ("GET", "HEAD") and request.headers.get("X-Encore") != "1":
        raise Refusal(403, "Missing request header")
    auth = svc().auth
    if auth.enabled and request.path not in OPEN_PATHS and not auth.valid(request.cookies.get(COOKIE)):
        if request.path.startswith("/api/"):
            return jsonify(error="Sign in first"), 401
        return redirect("/login")
    return None


def harden(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def create_app():
    DATA.mkdir(parents=True, exist_ok=True)
    PHOTOS.mkdir(exist_ok=True)
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    web = Web()
    settings = Settings(DATA / "settings.json")
    use_folders(settings)
    store = Store(DATA / "encore.db")
    plex = Plex(DATA / "plex.json", settings)
    s = SimpleNamespace(web=web, settings=settings, store=store, plex=plex, auth=Auth(DATA / "auth.json"),
                        owned=Owned(), filling=deque(), fill=threading.Event(), deezer=Deezer(web),
                        brainz=MusicBrainz(web, store), indexers=Indexers(settings, web),
                        sab=Sabnzbd(settings, web), library=Library(), started=False)

    def filed():
        try:
            sync_plex(s)
        except Exception as error:
            log.warning("Plex sync failed: %s", type(error).__name__)

    s.queue = Queue(s.indexers, s.sab, s.brainz, s.library, s.plex, s.store, filed)
    s.owned.load(store)
    app.extensions["encore"] = s
    app.register_blueprint(bp)
    app.before_request(guard)
    app.after_request(harden)

    @app.errorhandler(Refusal)
    def refused(error):
        return Response(error.text, status=error.status, content_type="text/plain; charset=utf-8")

    @app.errorhandler(SourceError)
    def source_failed(error):
        return jsonify(error=str(error)), 502

    return app


def start_workers(app):
    """The queue, the hourly Plex sync and the track-list filler. Once per process."""
    s = app.extensions["encore"]
    if s.started:
        return
    s.started = True
    s.queue.start()
    threading.Thread(target=plex_rhythm, args=(s,), name="encore-plex", daemon=True).start()
    threading.Thread(target=fill_albums, args=(s,), name="encore-filler", daemon=True).start()
