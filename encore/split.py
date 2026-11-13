"""Splitting one YouTube upload of a whole album into its tracks.

Where each track starts comes from the video's chapters (YouTube makes them from timestamps in the
description) when there's one per track, and otherwise from MusicBrainz's track lengths, each added
to where the last track started. Either way each start then moves to the nearest gap in the sound,
if there is one close by. Gaps alone would split gapless albums in the wrong places and quiet
passages in the middle of a track, so they only fine-tune.

Nothing here is final: the plan is shown to a person, who checks each join and nudges any that are off.
"""
import json
import math
import subprocess

STEP = 0.1                  # seconds per loudness reading
MIN_GAP = 0.3               # seconds below the floor that count as a gap
CHAPTER_REACH = 6           # how far a chapter mark may move to reach a gap
LENGTH_REACH = 15           # how far a start worked out from track lengths may move
SHORTEST = 1.0              # seconds: no track is cut shorter than this
BARS = 1200                 # waveform bars sent to the page
SILENT = -120.0
SHOWN_FLOOR = -60.0         # the quietest level the waveform draws above nothing
LEVELS = ("aresample=8000,asetnsamples=n=800:p=0,"
          "astats=metadata=1:reset=1:measure_overall=RMS_level:measure_perchannel=none,"
          "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-")


class SplitError(Exception):
    pass


def loudness(path):
    """The sound's level in dB, one reading every STEP seconds."""
    done = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-af", LEVELS, "-f", "null", "-"],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise SplitError("Encore couldn't read the sound in the download")
    levels = []
    for line in done.stdout.splitlines():
        if line.startswith("lavfi.astats.Overall.RMS_level="):
            value = line.split("=", 1)[1].strip()
            try:
                number = float(value)
            except ValueError:
                number = SILENT
            levels.append(SILENT if math.isinf(number) or math.isnan(number) else max(SILENT, number))
    if not levels:
        raise SplitError("The download has no sound")
    return levels


def floor_for(levels):
    """The level below which it counts as a gap: 30 dB under the music's usual loudness, never above -45 dB."""
    heard = sorted(level for level in levels if level > SILENT)
    if not heard:
        return -45.0
    loud = heard[int(len(heard) * 0.7)]
    return min(-45.0, loud - 30)


def find_gaps(levels, floor):
    """Every stretch quieter than floor for at least MIN_GAP, as (start, end) seconds."""
    gaps, start = [], None
    for place, level in enumerate([*levels, 0.0]):
        if level < floor:
            start = place if start is None else start
        elif start is not None:
            if (place - start) * STEP >= MIN_GAP:
                gaps.append((round(start * STEP, 2), round(place * STEP, 2)))
            start = None
    return gaps


def cut_in(gap):
    """Where a gap is cut: its middle, or a second before the sound comes back when the gap is long,
    so a track doesn't start with a long silence (it stays at the end of the one before)."""
    start, end = gap
    return round(max((start + end) / 2, end - 1.0), 2)


def nearest_gap(gaps, expected, reach, low, high):
    """The gap to cut at for a start expected at `expected`: the closest one within reach, preferring
    longer gaps a little, and never so close to its neighbours that a track would be under SHORTEST."""
    best = None
    for gap in gaps:
        at = cut_in(gap)
        if abs(at - expected) > reach or not (low + SHORTEST <= at <= high - SHORTEST):
            continue
        weight = abs(at - expected) - min(gap[1] - gap[0], 2.0)
        if best is None or weight < best[0]:
            best = (weight, at)
    return best[1] if best else None


def plan(levels, track_seconds, chapters):
    """Where each track starts and the last one ends: N tracks give N + 1 boundaries.

    track_seconds: MusicBrainz's length of each track (0 when unknown). chapters: [{start, title}].
    Returns {duration, cuts, marks, source, warnings}, where marks says for each start whether it
    came from a chapter or the lengths, whether a gap was found, and how far it moved."""
    duration = round(len(levels) * STEP, 2)
    count = len(track_seconds)
    gaps = find_gaps(levels, floor_for(levels))
    warnings = []
    usable = [c for c in chapters if 0 <= c["start"] < duration]
    from_chapters = len(usable) == count and count > 1
    if usable and not from_chapters:
        warnings.append(f"The video has {len(usable)} chapter{'' if len(usable) == 1 else 's'} and the album "
                        f"{count} tracks, so Encore went by the track lengths instead.")
    listed = sum(track_seconds)
    if listed and all(track_seconds) and abs(duration - listed) > max(10, listed * 0.02):
        gap = round(abs(duration - listed))
        warnings.append(f"The video is {gap // 60}:{gap % 60:02d} {'longer' if duration > listed else 'shorter'} "
                        f"than the album. Check the first and last tracks especially.")
    cuts = [round(usable[0]["start"], 2) if from_chapters else 0.0]
    marks = [{"from": "chapter" if from_chapters else "start", "gap": False, "moved": 0}]
    for place in range(1, count):
        if from_chapters:
            expected, reach, source = usable[place]["start"], CHAPTER_REACH, "chapter"
        else:
            last = track_seconds[place - 1]
            left = duration - cuts[-1]
            expected = cuts[-1] + (last or left / (count - place + 1))
            reach, source = LENGTH_REACH, "length"
        # Room for the tracks still to come, at SHORTEST each.
        high = duration - (count - place - 1) * SHORTEST
        expected = min(max(expected, cuts[-1] + SHORTEST), high - SHORTEST)
        found = nearest_gap(gaps, expected, reach, cuts[-1], high)
        at = found if found is not None else round(expected, 2)
        cuts.append(at)
        marks.append({"from": source, "gap": found is not None, "moved": round(at - expected, 1)})
    cuts.append(duration)
    return {"duration": duration, "cuts": cuts, "marks": marks, "source": "chapters" if from_chapters else "lengths",
            "warnings": warnings, "gaps": [cut_in(g) for g in gaps][:3000], "peaks": peaks(levels)}


def peaks(levels):
    """The waveform: the loudest reading in each of BARS slices, as 0-100."""
    size = max(1, math.ceil(len(levels) / BARS))
    shown = []
    for start in range(0, len(levels), size):
        level = max(levels[start:start + size])
        shown.append(max(0, min(100, round((level - SHOWN_FLOOR) / -SHOWN_FLOOR * 100))))
    return shown


def checked_cuts(cuts, count, duration):
    """Boundaries sent back from the page: count + 1 numbers, in order, inside the audio, no track under SHORTEST."""
    if not isinstance(cuts, list) or len(cuts) != count + 1:
        raise SplitError("The track starts don't match the track list")
    try:
        cuts = [round(float(c), 2) for c in cuts]
    except (TypeError, ValueError):
        raise SplitError("The track starts must be numbers") from None
    if any(math.isnan(c) or math.isinf(c) for c in cuts) or cuts[0] < 0 or cuts[-1] > duration + 0.05:
        raise SplitError("A track start is outside the video")
    if any(b - a < SHORTEST - 0.01 for a, b in zip(cuts, cuts[1:])):
        raise SplitError("Each track must be at least a second long, in order")
    return cuts


def cut(source, target, start, end):
    """One track out of the album's sound, copied without re-encoding (AAC cuts to within a frame, ~23 ms)."""
    done = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(source),
                           "-map", "0:a:0", "-vn", "-c:a", "copy", "-movflags", "+faststart", str(target)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    return done.returncode == 0


def save(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data))
    temp.replace(path)


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
