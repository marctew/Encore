"""Encore's own Plex sign-in, plus a server-side scan of one album folder.

Never run the CLI Plex Media Scanner: it deleted tracks during the library tidy.
Errors are logged by type only, and the token only ever travels in a header.
"""
import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

import aiohttp

WAIT_SECONDS = 300
TIMEOUT = aiohttp.ClientTimeout(total=20)

log = logging.getLogger("encore")


class Plex:
    def __init__(self, path, settings):
        self.path = path
        self.settings = settings
        self.state = json.loads(path.read_text()) if path.exists() else {}
        if "client_id" not in self.state:
            self.state["client_id"] = uuid.uuid4().hex
            self.save()

    def save(self):
        self.path.touch(mode=0o600)
        self.path.write_text(json.dumps(self.state))
        os.chmod(self.path, 0o600)

    @property
    def server(self):
        return self.settings.data["plex"]["url"]

    @property
    def linked(self):
        return bool(self.state.get("token"))

    def headers(self):
        headers = {"Accept": "application/json", "X-Plex-Product": "Encore", "X-Plex-Version": "1.0.0",
                   "X-Plex-Client-Identifier": self.state["client_id"], "X-Plex-Platform": "Web",
                   "X-Plex-Device-Name": "Encore"}
        if self.linked:
            headers["X-Plex-Token"] = self.state["token"]
        return headers

    async def request(self, method, url):
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers=self.headers()) as session:
            async with session.request(method, url) as response:
                response.raise_for_status()
                text = await response.text()
                return json.loads(text) if text.strip() else {}

    async def start_link(self):
        pin = await self.request("POST", "https://plex.tv/api/v2/pins?strong=true")
        params = urlencode({"clientID": self.state["client_id"], "code": pin["code"],
                            "context[device][product]": "Encore"})
        return {"id": pin["id"], "url": f"https://app.plex.tv/auth#?{params}"}

    async def check_link(self, pin_id):
        pin = await self.request("GET", f"https://plex.tv/api/v2/pins/{pin_id}")
        if not pin.get("authToken"):
            return False
        self.state["token"] = pin["authToken"]
        self.save()
        return True

    async def section(self):
        """Plex and Encore can mount the share under different paths, so the library is
        matched on the music folder's name, falling back to the first music library."""
        data = await self.request("GET", f"{self.server}/library/sections")
        name = Path(self.settings.data["folders"]["music"]).name
        found = [(section, location["path"].rstrip("/")) for section in data["MediaContainer"].get("Directory", [])
                 if section.get("type") == "artist" for location in section.get("Location", [])]
        if not found:
            raise LookupError("No music library in Plex")
        section, path = next((pair for pair in found if Path(pair[1]).name == name), found[0])
        return section["key"], path, section.get("refreshing", False)

    async def status(self):
        if not self.server:
            return "unset"
        if not self.linked:
            return "none"
        try:
            await self.section()
            return "ok"
        except Exception as error:
            log.warning("Plex status failed: %s", type(error).__name__)
            return "error"

    async def scan(self, relative):
        key, root, _ = await self.section()
        path = quote(f"{root}/{relative}", safe="")
        await self.request("GET", f"{self.server}/library/sections/{key}/refresh?path={path}")

    async def library(self):
        """Every artist and album Plex holds. Plex's type 8 is artists and 9 is albums."""
        key = (await self.section())[0]
        data = {}
        for kind in (8, 9):
            url = f"{self.server}/library/sections/{key}/all?type={kind}&includeGuids=1"
            data[kind] = (await self.request("GET", url))["MediaContainer"].get("Metadata", [])
        mbid = lambda item: next((g["id"][7:] for g in item.get("Guid", []) if g["id"].startswith("mbid://")), "")
        artists = [{"rating": a["ratingKey"], "name": a.get("title") or "", "mbid": mbid(a), "thumb": a.get("thumb") or ""}
                   for a in data[8]]
        albums = [{"rating": a["ratingKey"], "artist_rating": a.get("parentRatingKey"), "artist": a.get("parentTitle") or "",
                   "title": a.get("title") or "", "year": a.get("year"), "thumb": a.get("thumb") or ""} for a in data[9]]
        return artists, albums

    async def video_shown(self, rating, name):
        """Asks Plex to re-read the artist, then waits for the video among its extras."""
        await self.request("PUT", f"{self.server}/library/metadata/{rating}/refresh")
        for _ in range(WAIT_SECONDS // 5):
            await asyncio.sleep(5)
            data = await self.request("GET", f"{self.server}/library/metadata/{rating}?includeExtras=1")
            items = data["MediaContainer"].get("Metadata", [{}])[0].get("Extras", {}).get("Metadata", [])
            if any(name in part.get("file", "") for item in items for media in item.get("Media", [])
                   for part in media.get("Part", [])):
                return True
        return False

    async def image(self, path):
        """A square-ish thumbnail, resized by Plex. The token stays on this side."""
        url = f"{self.server}/photo/:/transcode?width=400&height=400&minSize=1&upscale=0&url={quote(path, safe='')}"
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers=self.headers()) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                return await response.read(), response.content_type

    async def found(self, artist, folder, tracks):
        """Counts tracks by file path across the library, because Plex's online match can
        change an album's title or put it under another artist."""
        key, root, refreshing = await self.section()
        prefix = f"{root}/{folder}/"
        count = start = 0
        while True:
            url = f"{self.server}/library/sections/{key}/all?type=10&X-Plex-Container-Start={start}&X-Plex-Container-Size=2000"
            items = (await self.request("GET", url))["MediaContainer"].get("Metadata", [])
            if not items:
                return count >= tracks and not refreshing
            start += len(items)
            for item in items:
                count += any(part["file"].startswith(prefix) for media in item.get("Media", [])
                             for part in media.get("Part", []))

    async def confirm(self, artist, folder, tracks):
        """Needs two clean reads in a row, because tracks can still arrive while Plex matches."""
        seen = 0
        for _ in range(WAIT_SECONDS // 5):
            await asyncio.sleep(5)
            seen = seen + 1 if await self.found(artist, folder, tracks) else 0
            if seen == 2:
                return True
        return False
