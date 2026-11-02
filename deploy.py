#!/usr/bin/env python3
"""Restarts Encore in a gap between checks. A running download resumes from the queue on start.

Swaps in static/index.new.html if there is one. Waits so a restart never lands mid-filing.
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "app/static"

for _ in range(1200):
    jobs = json.loads((ROOT / "data/jobs.json").read_text())
    if not any(j["state"] in ("verifying", "filing") for j in jobs):
        break
    time.sleep(0.5)
else:
    raise SystemExit("No gap found in ten minutes. Nothing changed.")

if (STATIC / "index.new.html").exists():
    shutil.move(STATIC / "index.new.html", STATIC / "index.html")
subprocess.run(["sudo", "-n", "systemctl", "restart", "encore"], check=True)
print("restarted")
