"""Where Encore keeps its data, and the few settings that come from the environment.

Everything else (indexers, SABnzbd, Plex, folders, the password) is set on the Settings page.
  ENCORE_DATA           data folder: settings, the queue, the database, photos (/var/lib/encore)
  ENCORE_PORT           port for the development server (3346); the service sets gunicorn's
  ENCORE_TRUSTED_PROXY  the one proxy (a Cloudflare tunnel) whose CF-Connecting-IP is believed
"""
import os
from pathlib import Path

DATA = Path(os.environ.get("ENCORE_DATA", "/var/lib/encore"))
PHOTOS = DATA / "photos"
PORT = int(os.environ.get("ENCORE_PORT", 3346))
TRUSTED_PROXY = os.environ.get("ENCORE_TRUSTED_PROXY", "")
