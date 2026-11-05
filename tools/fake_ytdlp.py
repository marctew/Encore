#!/usr/bin/env python3
"""A stand-in for yt-dlp, for tools/check_youtube.py. Answers from FAKE_YT/videos.json and makes
real (tiny) mp4 files with ffmpeg, so everything after the download runs for real.

videos.json: {"<id>": {"title", "channel", "duration", "fail": "<stderr text>", "slow": true}}
Every download is logged to FAKE_YT/downloads.log, and a slow one writes its pid to FAKE_YT/slow.pid.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ["FAKE_YT"])
VIDEOS = json.loads((HOME / "videos.json").read_text())


def entry(video_id):
    video = VIDEOS[video_id]
    return {"id": video_id, "title": video["title"], "channel": video["channel"], "duration": video["duration"]}


def main(args):
    if "--version" in args:
        print("2099.01.01")
        return 0
    if "--" not in args:
        print("ERROR: no URL after --", file=sys.stderr)
        return 2
    target = args[args.index("--") + 1]
    if target.startswith("ytsearch"):
        print(json.dumps({"entries": [entry(i) for i in VIDEOS if not VIDEOS[i].get("fail")]}))
        return 0
    video_id = target.rsplit("v=", 1)[-1]
    if video_id not in VIDEOS:
        print(f"ERROR: [youtube] {video_id}: Video unavailable", file=sys.stderr)
        return 1
    video = VIDEOS[video_id]
    if "-J" in args:
        print(json.dumps(entry(video_id)))
        return 0
    with open(HOME / "downloads.log", "a") as log:
        log.write(video_id + "\n")
    if video.get("fail"):
        print(f"ERROR: [youtube] {video_id}: {video['fail']}")
        return 1
    if video.get("slow"):
        (HOME / "slow.pid").write_text(str(os.getpid()))
        for step in range(300):
            print(f"  {step / 3:.1f}%", flush=True)
            time.sleep(0.1)
    folder = Path(args[args.index("-o") + 1]).parent
    seconds = video["duration"]
    print("  50.0%", flush=True)
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x90:rate=10:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-c:a", "aac", "-b:a", "128k",
                    "-shortest", str(folder / f"{video_id}.mp4")], check=True)
    print("100.0%", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
