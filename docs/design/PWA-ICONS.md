# Home screen icons and metadata

How every app installs cleanly to an iPhone or iPad home screen (and Android / Chrome): a proper icon, a short name and full-screen behaviour. It applies to every Flask app built to `APP-STANDARD.md`. Paths below are inside the app's package (`<app>/`, APP-STANDARD 2); adapt them if the stack differs.

> **For Claude Code:** read this with `DESIGN-SPEC.md` 10 (app identity) and 16 (phones). Where they overlap, this file says how the shared pieces are used, not a second way to do them: the icon comes from the app's `brand/` icon, `theme-color` comes from `partials/theme_head.html`, and the bottom nav already pads for the home indicator.

## 1. Required files

All under `<app>/static/`:

| File | Size | Purpose |
|---|---|---|
| `img/icon.svg` | any | The app icon (DESIGN-SPEC 10): the browser tab favicon, sidebar and top bar logo. Already linked by `templates/base.html`. |
| `brand/logo.svg` | 64 × 64 viewBox | **Source for every PNG.** The app icon with a **square** tile (see section 5). |
| `apple-touch-icon.png` | 180 × 180 | iOS home screen icon. **Opaque, square, no pre-rounded corners.** |
| `icon-192.png` | 192 × 192 | Android / Chrome manifest icon |
| `icon-512.png` | 512 × 512 | Android splash and maskable icon |
| `favicon.ico` | 32 × 32 | Fallback for old browsers |
| `manifest.json` | – | Web app manifest |

All PNGs and `favicon.ico` are generated from `brand/logo.svg` (section 6). Never hand-edit the PNGs. Commit them, so the app needs nothing extra at runtime.

## 2. `<head>` block

Add to the app's `templates/base.html`, next to what the shared shell already has, so every page carries it. Replace `APP_NAME`.

```html
<!-- already in the shared base.html -->
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{% include "partials/theme_head.html" %}  {# sets theme-color to --bg for the current theme #}
<link rel="icon" href="{{ url_for('static', filename='img/icon.svg') }}" type="image/svg+xml">

<!-- add -->
<link rel="icon" href="{{ url_for('static', filename='favicon.ico') }}" sizes="32x32">

<!-- iOS home screen -->
<link rel="apple-touch-icon" href="{{ url_for('static', filename='apple-touch-icon.png', v=app_version) }}">
<meta name="apple-mobile-web-app-title" content="APP_NAME">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">

<!-- Android / Chrome -->
<link rel="manifest" href="{{ url_for('static', filename='manifest.json', v=app_version) }}">
```

Notes:
- **Don't add a second `theme-color` meta.** `partials/theme_head.html` sets it to `--bg` for the current theme (`#090b11` dark, `#f5f6fa` light), so the status bar and splash blend in.
- **`apple-mobile-web-app-title`** is what appears under the icon. Keep it to 12 characters or fewer, or iOS truncates it. Without it, iOS uses `<title>`, which is "Page · App name" (DESIGN-SPEC 10) and too long. Usually it's the app's two-word name ("Binge Stats").
- **`apple-mobile-web-app-capable="yes"`** runs the app full-screen with no Safari chrome. The trade-off: no back button, and external links open in an in-app Safari sheet. Default to **yes** for dashboards and tools; set **no** for anything link-heavy.
- **`black-translucent`** lets content run under the status bar. It needs the safe-area padding in section 3, or the top of the page sits behind the clock.
- **`sizes` on `apple-touch-icon`** is ignored by modern iOS; one 180 × 180 file is enough.
- **`app_version`** comes from `du_flask` (DESIGN-SPEC 7.15) and busts the cache when the icon changes (section 7).

## 3. Safe areas

When running full-screen, the notch and home indicator need padding. The shell does this on its fixed bars, not on `body`:
- **Bottom nav:** `uniform.css` already pads it with `env(safe-area-inset-bottom)`.
- **Top bar:** `uniform.css` pads it with `env(safe-area-inset-top)` from v1.16. Tailwind apps (Binge Stats) add the same to their own top bar: `padding-top: calc(12px + env(safe-area-inset-top))`.
- **Landscape:** `.main` and the bars keep their 16px side padding; add `env(safe-area-inset-left/right)` only if content is cut off on a real device.

Don't pad `body` as well, or the bars get the inset twice.

### 3.1 Page height on phones

Three iPhone behaviours, all found in HomeForm (9 Nov 2026), and what `uniform.css` and `js/fit-screen.js` do about them:

- **Safari's `100vh` is too tall.** It's the height with the toolbars hidden, so a page that fits still scrolls a little.
  `uniform.css` uses `100dvh` (the height that's showing) where the browser has it.
- **As a home-screen app, `black-translucent` makes iOS report the screen short by the status bar's height** (about 43pt on
  a notched iPhone). A page shorter than the real screen then leaves the fixed bottom nav floating above the bottom edge,
  with a dark strip under it. Long pages hide it, because they're taller than the screen anyway. `uniform.css` adds
  `env(safe-area-inset-top)` back to the minimum height in `display-mode: standalone`.
- **Never stop a page scrolling with `overflow: hidden` on `html` or `body`.** In home-screen mode that shrinks what iOS
  counts as the screen, and the bottom nav lifts with it.

**A full-height page** (a list that scrolls inside its card, like a shopping list, with nothing else moving): put
`full-height` on `<html>`, load `js/fit-screen.js` (plain script, `defer`), and give the part that fills the rest the
class `fill` as a direct child of `.main`, with `overflow: auto` on its scrolling list. The script sets `--app-h` from
`window.innerHeight`, measured, plus the status bar in standalone mode, because iOS's CSS heights don't always match what's
showing. Check it on a real iPhone from the home screen: no emulator runs standalone mode.

## 4. `manifest.json`

```json
{
  "name": "APP_NAME",
  "short_name": "APP_NAME",
  "description": "One-line description",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "orientation": "any",
  "background_color": "#090b11",
  "theme_color": "#090b11",
  "icons": [
    { "src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png" },
    { "src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ]
}
```

- **Colours:** the manifest is static, so it uses the dark theme's `--bg` (`#090b11`), the default theme (DESIGN-SPEC 1).
- **iOS largely ignores the manifest** and uses the `<meta>` and `<link>` tags above; Android uses the manifest. Ship both.
- **MIME type:** Flask serves `static/manifest.json` correctly by default. Behind a stricter proxy, make sure it's `application/manifest+json` or `application/json`.

## 5. Icon design rules

The home screen icon is the app icon from DESIGN-SPEC 10, so the apps read as a set: the gradient in the app's accent family, one white glyph.
- **Square tile, solid background.** iOS and Android apply their own rounded mask. A pre-rounded or transparent icon renders with black corners. So `static/brand/logo.svg` is a copy of the app's icon from `brand/<app>-icon.svg` with the tile's `rx="16"` set to `rx="0"`, and nothing else changed. The tile `<rect>` must be its first element: cairosvg won't add a background, and a transparent export breaks the iOS icon.
- **Keep the glyph inside the central ~80%.** Android's maskable crop takes the outer edge, and the iOS corner radius eats about 10% per side. The template's 36 × 36 inner area (DESIGN-SPEC 10) is well inside that.
- **One simple glyph, flat shapes.** No text unless it's 1–2 letters: it's illegible at 60px.
- **Dark mode:** don't rely on it. Icons are static; the accent gradient works on light and dark wallpapers.

## 6. Generating the PNGs

Keep this as `tools/make_icons.py` in the app's repo. Run it from the repo root whenever `logo.svg` changes, then commit the results.

```python
#!/usr/bin/env python3
"""Generate the home screen icons from <app>/static/brand/logo.svg.

python tools/make_icons.py <app>/static
Needs cairosvg and Pillow (pip install cairosvg pillow). cairosvg needs the Cairo library:
apt install libcairo2 on Debian; on Windows, run it on the app's host instead.
"""
import io
import sys
from pathlib import Path

import cairosvg
from PIL import Image

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "static")
SRC = OUT / "brand" / "logo.svg"

TARGETS = {
    "apple-touch-icon.png": 180,
    "icon-192.png": 192,
    "icon-512.png": 512,
}

for name, size in TARGETS.items():
    cairosvg.svg2png(url=str(SRC), write_to=str(OUT / name), output_width=size, output_height=size)
    print(f"wrote {OUT / name} ({size}x{size})")

png = cairosvg.svg2png(url=str(SRC), output_width=32, output_height=32)
Image.open(io.BytesIO(png)).save(OUT / "favicon.ico", sizes=[(32, 32)])
print(f"wrote {OUT / 'favicon.ico'}")
```

## 7. Deployment gotchas

- **Safari caches `apple-touch-icon` aggressively.** An app already on the home screen will **not** pick up a new icon. Delete it and add it again.
- **Cloudflare Access or a login in front of the app:** Safari fetches the icon when it's added, using the current session, so Access normally works. If the home screen shows a screenshot of the page instead of the icon, the fetch failed: check the icon URL opens in Safari while signed in, and that it isn't 302'd to a login page. An app's own login must let `/static/` through without a session.
- **Content-Type:** PNGs must be served as `image/png`. Check the headers if a proxy or a `send_from_directory` override is involved.
- **Cache-bust on change:** the `v=app_version` in section 2 does this on each release. Bump it by hand (`v=2`) if the icon changes without one.
- **HTTPS is required** for Chrome to honour `manifest.json`. That's fine through a Cloudflare Tunnel; plain-HTTP LAN testing silently skips it.

## 8. Checklist for a new app

- [ ] `<app>/static/brand/logo.svg` committed: the app icon with a square tile (`rx="0"`), the tile rect first
- [ ] `tools/make_icons.py` run; all PNGs and `favicon.ico` committed
- [ ] `manifest.json` created, with `name`, `short_name` and colours set
- [ ] The `<head>` additions in `base.html`, with the right `APP_NAME` and no second `theme-color`
- [ ] Top bar and bottom nav pad for safe areas (section 3)
- [ ] Decided `apple-mobile-web-app-capable` yes or no for this app
- [ ] Tested: Safari → Share → Add to Home Screen gives the right icon, name and full-screen behaviour
- [ ] Tested on Android Chrome: the install prompt appears and the icon is right
