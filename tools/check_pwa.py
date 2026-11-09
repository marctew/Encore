"""Checks the home screen icons, the manifest and their <head> tags (Design Uniformity PWA-ICONS.md) on a
throwaway copy of the app, with a password set so the icons are shown to load without signing in."""
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="encore-check-"))
os.environ["ENCORE_DATA"] = str(ROOT)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from encore import __version__, engine  # noqa: E402
from encore import web as server  # noqa: E402

results = []


def expect(name, ok, detail=""):
    print("PASS" if ok else "FAIL", "|", name, "|", detail)
    results.append(bool(ok))


def main():
    engine.JOBS_FILE = ROOT / "jobs.json"
    engine.MUSIC, engine.IMPORT, engine.DOWNLOADS = ROOT / "Music", ROOT / "Import", ROOT / "Encore"
    for folder in (engine.MUSIC, engine.IMPORT, engine.DOWNLOADS):
        folder.mkdir()
    app = server.create_app()
    client = app.test_client()
    status = client.post("/api/password", json={"new": "correct horse"}, headers={"X-Encore": "1"}).status_code
    signed_in = client.get("/").get_data(as_text=True)
    client.delete_cookie(server.COOKIE)
    expect("password on, signed out", status == 200 and client.get("/").status_code == 302)

    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        r = client.get(f"/static/{name}")
        ok = r.status_code == 200 and r.mimetype == "image/png"
        img = Image.open(io.BytesIO(r.data)) if ok else None
        expect(f"{name} loads signed out, {size}x{size} and opaque", ok and img.size == (size, size) and img.mode == "RGB",
               (r.status_code, r.mimetype, img and (img.size, img.mode)))
    r = client.get("/static/favicon.ico")
    ok = r.status_code == 200 and r.mimetype.startswith("image/")
    expect("favicon.ico loads signed out, 32x32", ok and Image.open(io.BytesIO(r.data)).size == (32, 32), (r.status_code, r.mimetype))
    r = client.get("/static/img/icon.svg")
    expect("img/icon.svg loads signed out", r.status_code == 200 and r.mimetype == "image/svg+xml", r.status_code)

    r = client.get("/static/manifest.json")
    expect("manifest loads signed out, as JSON", r.status_code == 200 and r.mimetype in ("application/json", "application/manifest+json"),
           (r.status_code, r.mimetype))
    m = json.loads(r.data) if r.status_code == 200 else {}
    expect("manifest names Encore, standalone", m.get("name") == m.get("short_name") == "Encore" and m.get("display") == "standalone")
    expect("manifest colours are dark --bg", m.get("background_color") == m.get("theme_color") == "#090b11")
    icons = m.get("icons", [])
    expect("manifest icons", {i["src"] for i in icons} == {"/static/icon-192.png", "/static/icon-512.png"}
           and any(i.get("purpose") == "maskable" for i in icons), icons)
    expect("every manifest icon loads", all(client.get(i["src"]).status_code == 200 for i in icons))

    # Old addresses still work, for pages and phones that know them.
    r = client.get("/favicon.ico")
    expect("/favicon.ico redirects to the .ico", r.status_code == 301 and r.headers["Location"].split("?")[0].endswith("/static/favicon.ico"), r.headers.get("Location"))
    r = client.get("/static/icon.svg")
    expect("old /static/icon.svg redirects", r.status_code == 301 and r.headers["Location"].split("?")[0].endswith("/static/img/icon.svg"), r.headers.get("Location"))

    logo = (Path(server.__file__).parent / "static" / "brand" / "logo.svg").read_text()
    body = logo[logo.index("<svg"):]
    first = body[body.index(">") + 1:].lstrip()
    expect("logo.svg tile is square and first", first.startswith('<rect width="64" height="64" rx="0"'), first[:60])

    for where, html in (("app page", signed_in), ("login page", client.get("/login").get_data(as_text=True))):
        expect(f"{where}: apple-touch-icon, cache-busted", 'rel="apple-touch-icon" href="/static/apple-touch-icon.png?v=' in html)
        expect(f"{where}: home screen name", '<meta name="apple-mobile-web-app-title" content="Encore">' in html)
        expect(f"{where}: full screen", '<meta name="apple-mobile-web-app-capable" content="yes">' in html
               and '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">' in html)
        expect(f"{where}: manifest, cache-busted", 'rel="manifest" href="/static/manifest.json?v=' in html)
        expect(f"{where}: favicons", re.search(r'href="/static/img/icon\.svg\?v=\d+" type="image/svg\+xml"', html)
               and re.search(r'href="/static/favicon\.ico\?v=\d+" sizes="32x32"', html))
        expect(f"{where}: one theme-color", html.count('<meta name="theme-color"') == 1, html.count('<meta name="theme-color"'))
        # test_static_links_carry_a_version (APP-STANDARD 12): a changed file is a new address, so no cache
        # between here and a phone can serve the old copy. ?v=<mtime> from du_flask, or the home screen
        # icon and manifest's own ?v=<app version> (PWA-ICONS.md).
        links = re.findall(r'(?:href|src)="(/static/[^"]*)"', html)
        versioned = re.compile(rf"\?v=(\d+|{re.escape(__version__)})$")
        unversioned = [link for link in links if not versioned.search(link)]
        expect(f"{where}: static links carry a version", links and not unversioned, unversioned or "no static links")

    # Pages are never kept; static files are kept but revalidated (APP-STANDARD 12). The photos and cover
    # art keep their own max-age.
    client.post("/api/login", json={"password": "correct horse"}, headers={"X-Encore": "1"})
    r = client.get("/")
    expect("a page is no-store", r.status_code == 200 and r.mimetype == "text/html"
           and r.headers.get("Cache-Control") == "no-store", (r.status_code, r.headers.get("Cache-Control")))
    r = client.get("/login")
    expect("the sign-in page is no-store", r.headers.get("Cache-Control") == "no-store", r.headers.get("Cache-Control"))
    for name in ("encore.css", "design/uniform.css", "encore.js"):
        r = client.get(f"/static/{name}")
        expect(f"static {name} is no-cache", r.status_code == 200 and r.headers.get("Cache-Control") == "no-cache",
               (r.status_code, r.headers.get("Cache-Control")))
    etag = client.get("/static/encore.css").headers.get("ETag")
    r = client.get("/static/encore.css", headers={"If-None-Match": etag})
    expect("an unchanged static file answers 304, no-cache", r.status_code == 304 and r.headers.get("Cache-Control") == "no-cache",
           (r.status_code, r.headers.get("Cache-Control")))


main()
sys.exit(0 if all(results) else 1)
