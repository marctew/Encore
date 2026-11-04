"""The outside services Encore talks to: MusicBrainz, Newznab indexers, SABnzbd and Deezer.

Exception text from requests holds the full URL, which holds the API key, so only exception
types are logged, and nothing built from a URL ever reaches the page.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from .match import APOSTROPHES, words

log = logging.getLogger("encore")
AGENT = "Encore/2.0 (self-hosted music library tool)"
TIMEOUT = 40
MB = "https://musicbrainz.org/ws/2/"
CATEGORY = "encore"
CACHE_AGE = 600
BUSY = (429, 502, 503, 504)
AUDIO_CATEGORIES = "3000,3010,3040,3999"
VIDEO_CATEGORY = "3020"
PAGE_PAUSES = (2, 3, 5, 8, 12, 15, 15)
BACKGROUND_PAUSES = (5,)
DEFAULTS = {"indexers": [], "sab": {"url": "", "key": ""}, "plex": {"url": ""},
            "folders": {"music": "", "imports": "", "downloads": ""}}
ADDRESS = re.compile(r"^https?://[^\s/]+")


class SourceError(Exception):
    """Text is safe to show on the page."""


class Settings:
    def __init__(self, path):
        self.path = path
        saved = json.loads(path.read_text()) if path.exists() else {}
        self.data = {**json.loads(json.dumps(DEFAULTS)), **saved}

    def save(self):
        temp = self.path.with_suffix(".tmp")
        with open(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
            json.dump(self.data, out, indent=1)
        temp.replace(self.path)

    def public(self):
        return {"indexers": [{"name": i["name"], "url": i["url"], "has_key": bool(i["key"])}
                             for i in self.data["indexers"]],
                "sab": {"url": self.data["sab"]["url"], "has_key": bool(self.data["sab"]["key"])},
                "plex": self.data["plex"], "folders": self.data["folders"]}

    def update(self, body):
        """A blank key keeps the saved one, so the page never needs to hold a key."""
        old = {i["name"]: i["key"] for i in self.data["indexers"]}
        indexers = []
        for item in body.get("indexers") or []:
            name, url = str(item.get("name", "")).strip(), str(item.get("url", "")).strip().rstrip("/")
            if not name:
                continue
            if not ADDRESS.match(url):
                raise SourceError(f"{name} needs a web address")
            indexers.append({"name": name, "url": url, "key": str(item.get("key") or "").strip() or old.get(name, "")})
        sab = body.get("sab") or {}
        url = str(sab.get("url", "")).strip().rstrip("/")
        if url and not ADDRESS.match(url):
            raise SourceError("SABnzbd needs a web address")
        plex = str((body.get("plex") or self.data["plex"]).get("url", "")).strip().rstrip("/")
        if plex and not ADDRESS.match(plex):
            raise SourceError("Plex needs a web address")
        chosen = body.get("folders") or self.data["folders"]
        folders = {name: str(chosen.get(name, "")).strip() for name in DEFAULTS["folders"]}
        for name, value in folders.items():
            if value and not value.startswith("/"):
                raise SourceError(f"The {name} folder needs a full path, starting with /")
        self.data = {"indexers": indexers, "plex": {"url": plex}, "folders": folders,
                     "sab": {"url": url, "key": str(sab.get("key") or "").strip() or self.data["sab"]["key"]}}
        self.save()


class Web:
    """One requests session per thread: sessions keep connections open, but aren't safe to share."""

    def __init__(self):
        self.local = threading.local()

    def session(self):
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers["User-Agent"] = AGENT
        return self.local.session

    def get(self, url, **kwargs):
        return self.session().get(url, timeout=TIMEOUT, **kwargs)

    def post(self, url, **kwargs):
        return self.session().post(url, timeout=TIMEOUT, **kwargs)

    def close(self):
        pass


def credit(names):
    return "".join(n.get("name", "") + n.get("joinphrase", "") for n in names or []).strip()


def lucene(text):
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/&|])', r"\\\1", text)


def cover(album_id, size=250):
    return f"https://coverartarchive.org/release-group/{album_id}/front-{size}"


class MusicBrainz:
    """Asks for at most one request a second, as MusicBrainz requires."""

    def __init__(self, web, store):
        self.web = web
        self.store = store
        self.lock = threading.Lock()
        self.count = threading.Lock()
        self.last = 0
        self.busy = 0

    def get(self, path, background=False, **params):
        """busy counts what the page is waiting on, so background filling steps aside for it.
        MusicBrainz often answers 503 "busy" well under its rate limit, so a page request keeps
        trying for about a minute, and the lock is free meanwhile."""
        params["fmt"] = "json"
        with self.count:
            self.busy += not background
        try:
            for pause in (*(BACKGROUND_PAUSES if background else PAGE_PAUSES), None):
                with self.lock:
                    time.sleep(max(0, self.last + 1.1 - time.time()))
                    self.last = time.time()
                    try:
                        response = self.web.get(MB + path, params=params)
                        if response.status_code == 404:
                            raise SourceError("MusicBrainz doesn't know that")
                        if response.status_code in BUSY:
                            log.info("MusicBrainz said %s", response.status_code)
                        else:
                            response.raise_for_status()
                            return response.json()
                    except requests.Timeout:
                        log.warning("MusicBrainz timed out")
                    except (requests.RequestException, ValueError) as error:
                        log.warning("MusicBrainz call failed: %s", type(error).__name__)
                if pause is None:
                    break
                time.sleep(pause)
            raise SourceError("MusicBrainz stayed busy for a minute. Try again shortly.")
        finally:
            with self.count:
                self.busy -= not background

    @staticmethod
    def summary(group):
        date = group.get("first-release-date") or ""
        extras = group.get("secondary-types") or []
        lead = ((group.get("artist-credit") or [{}])[0].get("artist") or {}).get("id") or ""
        return {"id": group["id"], "title": " ".join((group.get("title") or "Unknown album").split()),
                "artist": credit(group.get("artist-credit")) or "Unknown artist", "artist_id": lead, "year": date[:4],
                "kind": " ".join([*extras, group.get("primary-type") or "Album"]), "cover": cover(group["id"])}

    def search(self, text):
        terms = [lucene(word) for word in text.split()]
        query = " AND ".join(f"(releasegroup:{t} OR artist:{t})" for t in terms)
        data = self.get("release-group", query=f"({query}) AND (primarytype:album OR primarytype:ep)", limit=60)
        return [self.summary(g) for g in data.get("release-groups") or []]

    @staticmethod
    def artist_info(artist):
        span = artist.get("life-span") or {}
        tags = sorted(artist.get("tags") or [], key=lambda t: -(t.get("count") or 0))
        return {"id": artist["id"], "name": artist.get("name") or "Unknown artist", "kind": artist.get("type") or "",
                "country": (artist.get("area") or {}).get("name") or artist.get("country") or "",
                "begin": (span.get("begin") or "")[:4], "end": (span.get("end") or "")[:4], "ended": bool(span.get("ended")),
                "note": artist.get("disambiguation") or "", "tags": [t["name"] for t in tags[:4]]}

    def artists(self, text):
        data = self.get("artist", query=f'artist:"{lucene(text)}"', limit=30)
        return [self.artist_info(a) for a in data.get("artists") or []]

    def artist(self, artist_id):
        return self.artist_info(self.get(f"artist/{artist_id}", inc="tags"))

    def artist_albums(self, artist_id):
        groups = []
        while True:
            data = self.get("release-group", artist=artist_id, type="album|ep", limit=100, offset=len(groups),
                            inc="artist-credits", **{"release-group-status": "website-default"})
            found = data.get("release-groups") or []
            groups += found
            if not found or len(groups) >= data.get("release-group-count", 0):
                break
        albums = [self.summary(g) for g in groups]
        return [a for a in albums if a["artist_id"] == artist_id]

    @staticmethod
    def edition(release):
        tracks = []
        for medium in release.get("media") or []:
            if re.search(r"DVD|Blu-ray|VHS", medium.get("format") or ""):
                continue
            for track in medium.get("tracks") or []:
                tracks.append({"disc": medium.get("position") or 1, "number": track.get("position") or 0,
                               "title": track.get("title") or "Untitled", "seconds": (track.get("length") or 0) // 1000,
                               "artist": credit(track.get("artist-credit"))})
        return {"id": release["id"], "title": release.get("title") or "", "date": release.get("date") or "",
                "status": release.get("status") or "", "tracks": tracks,
                "label": next((i["label"].get("name") for i in release.get("label-info") or [] if i.get("label")), "")}

    def album(self, album_id, refresh=False, background=False):
        saved = None if refresh else self.store.details(album_id)
        if saved:
            return saved
        group = self.get(f"release-group/{album_id}", background=background, inc="artist-credits")
        details = self.summary(group)
        releases = []
        while True:
            data = self.get("release", background=background, **{"release-group": album_id},
                            inc="recordings artist-credits labels", limit=100, offset=len(releases))
            found = data.get("releases") or []
            releases += found
            if not found or len(releases) >= data.get("release-count", 0) or len(releases) >= 300:
                break
        editions = [e for e in map(self.edition, releases) if e["tracks"]]
        official = [e for e in editions if e["status"] == "Official"] or editions
        if not official:
            raise SourceError("MusicBrainz has no track list for this album")
        sizes = [len(e["tracks"]) for e in official]
        usual = max(set(sizes), key=lambda size: (sizes.count(size), -size))
        main = min((e for e in official if len(e["tracks"]) == usual), key=lambda e: e["date"] or "9999")
        details.update(editions=official, tracks=len(main["tracks"]), tracks_list=main["tracks"],
                       seconds=sum(t["seconds"] for t in main["tracks"]), label=main["label"],
                       released=main["date"] or details["year"], discs=len({t["disc"] for t in main["tracks"]}),
                       minutes=sum(t["seconds"] for t in main["tracks"]) // 60, cover=cover(album_id, 500))
        self.store.save_details(album_id, details)
        return {**details, "saved_at": time.time()}


class Deezer:
    """Artist photos for anyone Plex doesn't know. Deezer needs no key, but matches on name alone."""

    def __init__(self, web):
        self.web = web

    def photo(self, name):
        try:
            found = self.web.get("https://api.deezer.com/search/artist", params={"q": name, "limit": 10}).json().get("data") or []
            same = [a for a in found if words(a.get("name")) == words(name) and "/artist//" not in (a.get("picture_big") or "/artist//")]
            if not same:
                return None
            link = max(same, key=lambda a: a.get("nb_fan") or 0)["picture_big"]
            if urlsplit(link).hostname.rsplit(".", 2)[-2:] != ["dzcdn", "net"]:
                return None
            response = self.web.get(link)
            return response.content if response.status_code == 200 else None
        except (requests.RequestException, ValueError) as error:
            log.warning("Deezer call failed: %s", type(error).__name__)
            return None


def spellings(artist, title):
    """Indexers differ on apostrophes: some only find We Can't Dance as "We Can t Dance"."""
    title = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", title) or title
    text = re.sub(f"[{APOSTROPHES}]", "'", f"{artist} {title}")
    tidy = lambda value: " ".join(re.sub(r"[^\w\s']", " ", value, flags=re.UNICODE).split())
    forms = [" ".join(text.split()), tidy(text).replace("'", " "), " ".join(tidy(text).replace("'", "").split())]
    return list(dict.fromkeys(forms))


def attrs(item):
    found = {}
    for child in item:
        if child.tag.endswith("attr"):
            found.setdefault(child.get("name"), []).append(child.get("value"))
    return found


class Indexers:
    def __init__(self, settings, web):
        self.settings = settings
        self.web = web
        self.cache = {}

    def active(self):
        return [i for i in self.settings.data["indexers"] if i["key"]]

    def call(self, indexer, **params):
        params = {"apikey": indexer["key"], **params}
        try:
            response = self.web.get(indexer["url"] + "/api", params=params)
            body, status = response.content, response.status_code
        except requests.RequestException as error:
            log.warning("%s call failed: %s", indexer["name"], type(error).__name__)
            raise SourceError(f"{indexer['name']} isn't answering") from None
        if status == 429:
            raise SourceError(f"{indexer['name']} says Encore has hit its request limit")
        if status in (401, 403):
            raise SourceError(f"{indexer['name']} refused the API key")
        return status, body

    def query(self, indexer, text, cat=AUDIO_CATEGORIES, offset=0):
        key = (indexer["name"], text, cat, offset)
        cached = self.cache.get(key)
        if cached and time.time() - cached[0] < CACHE_AGE:
            return cached[1]
        status, body = self.call(indexer, t="search", cat=cat, extended=1, limit=100, offset=offset, q=text)
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise SourceError(f"{indexer['name']} sent something unreadable") from None
        if root.tag == "error":
            raise SourceError(f"{indexer['name']}: {root.get('description') or 'it refused the search'}")
        items = []
        for item in root.iter("item"):
            extra = attrs(item)
            guid = (extra.get("guid") or [(item.findtext("guid") or "").rstrip("/").rsplit("/", 1)[-1]])[0]
            size = int((extra.get("size") or [0])[0] or 0)
            if not guid:
                continue
            try:
                posted = parsedate_to_datetime(item.findtext("pubDate")).timestamp()
            except (TypeError, ValueError):
                posted = 0
            items.append({"indexer": indexer["name"], "guid": guid, "title": item.findtext("title") or "",
                          "size": size, "categories": extra.get("category") or [], "posted": posted,
                          "grabs": int((extra.get("grabs") or [0])[0] or 0),
                          "password": (extra.get("password") or ["0"])[0] not in ("0", "")})
        self.cache[key] = (time.time(), items)
        return items

    def search(self, artist, title):
        """Returns (items, problems). One indexer failing never loses the other's results."""
        forms = spellings(artist, title)

        def one(indexer):
            found = []
            for form in forms:
                found += self.query(indexer, form)
            return found

        return self.pool(one)

    def videos(self, artist):
        """Searches by artist alone: indexers rarely find a video by its song title."""
        forms = spellings(artist, "")

        def one(indexer):
            found = []
            for form in forms:
                for offset in (0, 100, 200):
                    page = self.query(indexer, form, VIDEO_CATEGORY, offset)
                    found += page
                    if len(page) < 100:
                        break
            return found

        return self.pool(one)

    def pool(self, one):
        """Every indexer at once, each in its own thread."""
        indexers = self.active()
        pooled, problems = [], []
        if indexers:
            with ThreadPoolExecutor(len(indexers)) as workers:
                futures = [workers.submit(one, indexer) for indexer in indexers]
            for future in futures:
                try:
                    pooled += future.result()
                except SourceError as error:
                    problems.append(str(error))
                except Exception as error:
                    log.warning("Indexer search failed: %s", type(error).__name__)
                    problems.append("An indexer search failed")
        seen, items = set(), []
        for item in pooled:
            key = (" ".join(item["title"].casefold().split()), item["size"])
            if key not in seen:
                seen.add(key)
                item["key"] = hashlib.sha1(f"{item['indexer']}|{item['guid']}".encode()).hexdigest()[:16]
                items.append(item)
        return items, problems

    def fetch(self, name, guid):
        indexer = next((i for i in self.active() if i["name"] == name), None)
        if not indexer:
            raise SourceError(f"{name} isn't set up in Settings any more")
        status, body = self.call(indexer, t="get", id=guid)
        if status != 200 or b"<nzb" not in body[:4000]:
            match = re.search(rb'description="([^"]{1,120})"', body[:2000])
            detail = match.group(1).decode(errors="replace") if match else "it didn't send the NZB"
            raise SourceError(f"{name}: {detail}")
        return body


class Sabnzbd:
    def __init__(self, settings, web):
        self.settings = settings
        self.web = web

    @property
    def ready(self):
        return bool(self.settings.data["sab"]["key"] and self.settings.data["sab"]["url"])

    def call(self, mode, files=None, **params):
        sab = self.settings.data["sab"]
        params = {"mode": mode, "output": "json", "apikey": sab["key"], **params}
        try:
            response = self.web.post(sab["url"] + "/api", params=params, files=files)
            if response.status_code != 200:
                raise SourceError("SABnzbd refused the request")
            result = response.json()
        except (requests.RequestException, ValueError) as error:
            log.warning("SABnzbd call failed: %s", type(error).__name__)
            raise SourceError("SABnzbd isn't answering") from None
        if isinstance(result, dict) and result.get("status") is False:
            raise SourceError(f"SABnzbd: {result.get('error') or 'it refused the request'}")
        return result

    def version(self):
        return self.call("version").get("version")

    def ensure_category(self):
        """Encore's own category, so nothing else's downloads are ever touched."""
        categories = self.call("get_cats").get("categories") or []
        if CATEGORY not in categories:
            folder = self.settings.data["folders"]["downloads"]
            self.call("set_config", section="categories", name=CATEGORY, dir=folder, pp=3)

    def add(self, nzb, name):
        self.ensure_category()
        result = self.call("addfile", files={"name": (f"{name}.nzb", nzb, "application/x-nzb")}, cat=CATEGORY, nzbname=name)
        ids = result.get("nzo_ids") or []
        if not ids:
            raise SourceError("SABnzbd wouldn't take the NZB")
        return ids[0]

    def state(self, nzo):
        """Returns ("queue", percent), ("working", status), ("done", folder), ("failed", why) or None."""
        queue = self.call("queue", nzo_ids=nzo).get("queue") or {}
        for slot in queue.get("slots") or []:
            if slot.get("nzo_id") == nzo:
                return "queue", float(slot.get("percentage") or 0)
        history = self.call("history", nzo_ids=nzo).get("history") or {}
        for slot in history.get("slots") or []:
            if slot.get("nzo_id") != nzo:
                continue
            status = slot.get("status")
            if status == "Completed":
                return "done", slot.get("storage") or ""
            if status == "Failed":
                return "failed", slot.get("fail_message") or "SABnzbd couldn't complete it"
            return "working", status or "Finishing"
        return None

    def forget(self, nzo):
        for mode in ("queue", "history"):
            try:
                self.call(mode, name="delete", value=nzo, del_files=1)
            except SourceError:
                pass
