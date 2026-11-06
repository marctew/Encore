#!/usr/bin/env python3
"""Generate the home screen icons from <app>/static/brand/logo.svg.

python tools/make_icons.py encore/static
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
