#!/usr/bin/env python3
"""Check a running app against the outside API standard (API-STANDARD.md).

  python3 tools/check_api.py <base_url> <read_token> [write_token] [app_token]
  python3 tools/check_api.py http://127.0.0.1:8181 bs_xxx

Read-only unless an app token is given: a write token is only sent in the URL, to check it's
refused. With an app token (an app that offers inbound items, API standard draft 4), it also runs
one item through PUT, PUT again, GET, done, a reopening PUT and DELETE, on a throwaway key
(check-api-<random>), so it leaves one archived item behind.
Standard library only, so it runs on any host. Prints one line per check and exits 1 if any fail.
Run it after each app's change, like check_du_sync.py. Lifted from Binge Stats' pilot.
"""
import json
import re
import sys
import urllib.error
import urllib.request

if len(sys.argv) < 3:
    print("Usage: check_api.py <base_url> <read_token> [write_token] [app_token]", file=sys.stderr)
    sys.exit(2)
base, tok = sys.argv[1].rstrip("/"), sys.argv[2]
wtok = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else None
atok = sys.argv[4] if len(sys.argv) > 4 else None
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
STATES = {"ok", "busy", "warning", "error", "off"}
failed = 0


def get(path, token=tok, headers=None, url_token=False, read=True):
    url = base + path
    h = dict(headers or {})
    if token and url_token:
        url += ("&" if "?" in url else "?") + "token=" + token
    elif token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    return r.status, dict(r.headers), (r.read() if read else r)


def check(name, ok, detail=""):
    global failed
    failed += not ok
    print(("  ok  " if ok else "  FAIL") + f"  {name}" + (f": {detail}" if detail and not ok else ""))
    return ok


def js(body):
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return None


print(f"Checking {base}")
code, h, body = get("/api/v1/meta")
meta = js(body) or {}
check("meta answers 200 JSON", code == 200 and isinstance(meta, dict), f"HTTP {code}")
need = {"app", "name", "version", "sha", "api", "accent", "icon", "time_zone", "feeds", "actions", "displays", "events"}
check("meta has every field", need <= set(meta), f"missing {sorted(need - set(meta))}")
check("meta api is 1", meta.get("api") == 1, repr(meta.get("api")))
check("meta lists are lists", all(isinstance(meta.get(k), list) for k in ("feeds", "actions", "displays", "events")))

etag = h.get("ETag")
check("GETs carry an ETag", bool(etag))
if etag:
    code2, _h, body2 = get("/api/v1/meta", headers={"If-None-Match": etag})
    check("If-None-Match gives 304 with no body", code2 == 304 and not body2, f"HTTP {code2}")

code, h, body = get("/api/v1/status")
st = js(body) or {}
check("status answers 200 JSON", code == 200, f"HTTP {code}")
check("status state is one of " + "/".join(sorted(STATES)), st.get("state") in STATES, repr(st.get("state")))
check("status headline is text", isinstance(st.get("headline"), str) and st.get("headline"))
counts = st.get("counts")
check("status counts: at most 6, each key/label/value", isinstance(counts, list) and len(counts) <= 6 and
      all({"key", "label", "value"} <= set(c) for c in counts))
check("status generated_at is UTC ...Z", bool(ISO.match(str(st.get("generated_at")))), repr(st.get("generated_at")))
check("status refresh_s is a number", isinstance(st.get("refresh_s"), int))
check("status is under 2 KB", len(body) < 2048, f"{len(body)} bytes")
check("status has Cache-Control", "max-age" in (h.get("Cache-Control") or ""))

code, _h, _b = get("/api/v1/status", url_token=True)
check("a read token works in the URL", code == 200, f"HTTP {code}")
code, _h, body = get("/api/v1/status", token="bs_not_a_real_token")
err = js(body) or {}
check("a bad token gets 401 {error, code}", code == 401 and isinstance(err.get("error"), str) and err.get("code") == "unauthorised",
      f"HTTP {code} {body[:80]!r}")
if wtok:
    code, _h, body = get("/api/v1/status", token=wtok, url_token=True)
    check("a write token in the URL gets 400", code == 400, f"HTTP {code}")

image = None
for f in meta.get("feeds") or []:
    code, _h, body = get(f"/api/v1/feeds/{f['name']}?limit=5")
    fd = js(body) or {}
    items = fd.get("items")
    ok = code == 200 and {"feed", "title", "items", "next_cursor", "generated_at", "refresh_s"} <= set(fd) and isinstance(items, list)
    bad = [i for i in items or [] if not (isinstance(i.get("id"), str) and isinstance(i.get("title"), str))
           or ("at" in i and not ISO.match(str(i["at"]))) or None in i.values()]
    check(f"feed {f['name']}: shape and {len(items or [])} item(s)", ok and not bad, f"HTTP {code}, {len(bad)} bad item(s)")
    image = image or next((i["image"] for i in items or [] if i.get("image")), None)
code, _h, _b = get("/api/v1/feeds/no_such_feed")
check("an unknown feed gets 404", code == 404, f"HTTP {code}")

if image:
    code, h, body = get(image + ("&" if "?" in image else "?") + "w=160", headers={"Accept": "image/webp"})
    check("images: WebP when accepted", code == 200 and h.get("Content-Type") == "image/webp", f"HTTP {code} {h.get('Content-Type')}")
    code, h, body = get(image + ("&" if "?" in image else "?") + "w=160", headers={"Accept": "image/jpeg"}, url_token=True)
    check("images: JPEG otherwise, URL token works", code == 200 and h.get("Content-Type") == "image/jpeg", f"HTTP {code} {h.get('Content-Type')}")
else:
    print("  --    images: no feed item had one to try")

if meta.get("events"):
    code, h, resp = get("/api/v1/events", read=False)
    if check("events stream answers text/event-stream", code == 200 and (h.get("Content-Type") or "").startswith("text/event-stream"),
             f"HTTP {code}"):
        first = resp.readline().decode()
        check("events stream starts with retry:", first.startswith("retry:"), repr(first))
        resp.close()

for d in meta.get("displays") or []:
    code, h, body = get(f"/display/{d['name']}?static=1", url_token=True)
    check(f"display {d['name']}: 200 HTML, no referrer, no script with static=1",
          code == 200 and (h.get("Content-Type") or "").startswith("text/html") and h.get("Referrer-Policy") == "no-referrer"
          and b"<script" not in body, f"HTTP {code}")
    code, h, _b = get(f"/display/{d['name']}", token=None)
    check(f"display {d['name']}: no token gets a 401 page", code == 401 and (h.get("Content-Type") or "").startswith("text/html"),
          f"HTTP {code}")

def call(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, js(r.read()) or {}
    except urllib.error.HTTPError as e:
        return e.code, js(e.read()) or {}


if meta.get("inbound"):
    if not atok:
        print("  --    inbound: give an app token as the 4th argument to check it")
    else:
        import secrets
        from datetime import date, timedelta
        key = "check-api-" + secrets.token_hex(4)
        path = f"/api/v1/inbound/items/{key}"
        d1, d2 = (date.today() + timedelta(days=30)).isoformat(), (date.today() + timedelta(days=60)).isoformat()
        code, a = call("PUT", path, atok, {"title": "check_api test item (safe to ignore)", "due": f"{d1} 09:00"})
        check("inbound: a new key creates it (201)", code == 201 and a.get("key") == key and a.get("status") == "open",
              f"HTTP {code} {a}")
        first_id = a.get("id")
        check("inbound: due comes back local, with due_at in UTC", a.get("due") == f"{d1} 09:00"
              and bool(ISO.match(str(a.get("due_at")))), repr((a.get("due"), a.get("due_at"))))
        code, a = call("PUT", path, atok, {"title": "check_api test item (safe to ignore)", "due": f"{d1} 09:00"})
        check("inbound: the same key again updates, no duplicate (200)", code == 200 and a.get("id") == first_id,
              f"HTTP {code} id {a.get('id')} vs {first_id}")
        code, a = call("GET", path, atok)
        check("inbound: GET reads it", code == 200 and a.get("status") == "open", f"HTTP {code}")
        code, a = call("POST", path + "/done", atok, {})
        check("inbound: done marks it done", code == 200 and a.get("status") == "done", f"HTTP {code} {a.get('status')}")
        code, a = call("PUT", path, atok, {"due": f"{d1} 09:00"})
        check("inbound: the same due date leaves it done", code == 200 and a.get("status") == "done", f"HTTP {code} {a.get('status')}")
        code, a = call("PUT", path, atok, {"due": f"{d2} 09:00"})
        check("inbound: a later due date reopens it", code == 200 and a.get("status") == "open", f"HTTP {code} {a.get('status')}")
        code, a = call("DELETE", path, atok)
        check("inbound: DELETE archives it", code == 200 and a.get("status") == "archived", f"HTTP {code} {a.get('status')}")
        code, _a = call("GET", "/api/v1/inbound/items/check-api-never-made", atok)
        check("inbound: an unknown key gets 404", code == 404, f"HTTP {code}")
        code, a = call("PUT", "/api/v1/inbound/items/has%20space", atok, {"title": "x"})
        check("inbound: a bad key gets 400", code == 400, f"HTTP {code}")
        if wtok:
            code, a = call("PUT", path, wtok, {"title": "x"})
            check("inbound: a plain write token gets 403", code == 403 and a.get("code") == "forbidden", f"HTTP {code}")
        code, a = call("PUT", path, tok, {"title": "x"})
        check("inbound: a read token gets 403", code == 403, f"HTTP {code}")

print("All good." if not failed else f"{failed} check(s) failed.")
sys.exit(1 if failed else 0)
