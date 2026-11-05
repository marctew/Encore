"""Decides which indexer results really are the album, and which download matches which tracks.

Usenet titles are free text, so nothing here parses a format. It looks for the artist and the
album anywhere in the title, ignoring punctuation and spacing, then lets size settle the rest:
"We Can.t Dance", "We_Cant_Dance" and "We Can't Dance" all match, and a post named after its
first track still counts as the album when it is big enough to hold the album.
"""
import re
import unicodedata
from difflib import SequenceMatcher

APOSTROPHES = "'’‘ʼ`´′"
HIRES = re.compile(r"(?<!\d)(16|24)[ ._/-]*(?:bits?)?[ ._/-]*(44(?:[.,]1)?|48|88(?:[.,]2)?|96|176(?:[.,]4)?|192)(?:\s*k(?:hz)?)?(?![\d])", re.I)
BIT_24 = re.compile(r"(?<!\d)24[ ._-]?bit|hi[ -]?res", re.I)
LOSSY = {"mp3", "aac", "m4a", "ogg", "opus", "wma", "v0", "v2", "320", "256", "192kbps", "320kbps", "cbr", "vbr"}
OTHER = {"ape", "wavpack", "wv", "alac", "wav", "aiff", "dsd", "dsf", "dff", "sacd"}
SURROUND = {"dts", "ac3", "multichannel", "surround", "atmos"}
NOISE = {
    "flac", "mp3", "web", "webflac", "cd", "cdda", "cdrip", "lp", "vinyl", "remaster", "remastered", "deluxe",
    "edition", "version", "expanded", "anniversary", "special", "limited", "bonus", "tracks", "reissue", "bit",
    "24bit", "16bit", "24", "16", "44", "48", "88", "96", "176", "192", "1", "2", "4", "khz", "44khz", "96khz",
    "48khz", "192khz", "qobuz", "tidal", "hdtracks", "deezer", "lossless", "hires", "album", "proper", "repack",
    "disc", "cd1", "cd2", "rar", "part", "part01", "nzb", "par2", "nfo", "yenc", "file", "of", "and", "the", "by",
    "complete", "full", "stereo", "original", "mix", "th",
}
MIN_KBPS = 350
HIRES_MIN_KBPS = 800
SEQUEL = {"ii", "iii", "iv", "v", "vi", "vii", "viii", "2", "3", "4", "5"}
MAX_KBPS = 12000
UNKNOWN_MIN_KBPS = 500


def fold(text):
    text = (text or "").replace("Â", "")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()
    return re.sub(f"[{APOSTROPHES}]", "", text).replace("&", " and ")


def words(text):
    return re.findall(r"[^\W_]+", fold(text))


def find_span(tokens, target, start=0):
    """Finds target in tokens by joined letters, so ["can", "t"] equals ["cant"]."""
    wanted = "".join(target)
    if not wanted:
        return None
    for first in range(start, len(tokens)):
        joined = ""
        for last in range(first, len(tokens)):
            joined += tokens[last]
            if joined == wanted:
                return first, last + 1
            if len(joined) >= len(wanted) or not wanted.startswith(joined):
                break
    return None


def locate(tokens, name):
    target = words(name)
    span = find_span(tokens, target)
    if not span and target[:1] == ["the"]:
        span = find_span(tokens, target[1:])
    return span


# Albums named with only a symbol (Ed Sheeran's + × ÷ = −) have no words to look for, and releases
# usually spell them out, so the spoken name is looked for too.
SPOKEN = {"+": "plus", "×": "multiply", "÷": "divide", "=": "equals", "−": "subtract", "-": "subtract", "–": "subtract"}


def album_forms(title):
    forms = [title]
    if not words(title) and title.strip() in SPOKEN:
        forms.append(SPOKEN[title.strip()])
    short = re.split(r"\s*[:(\[]", title)[0].strip()
    if short and short != title and len(words(short)) >= 1:
        forms.append(short)
    return forms


def format_of(title, tokens, categories):
    found = set(tokens)
    if found & SURROUND or re.search(r"(?<!\d)5[ ._]1(?!\d)", title):
        return "surround"
    if "flac" in found or any(t.endswith("flac") for t in tokens):
        return "flac"
    if found & OTHER or "sa cd" in " ".join(tokens):
        return "other"
    if found & LOSSY:
        return "lossy"
    if "3040" in categories:
        return "flac"
    if "3010" in categories:
        return "lossy"
    return "unknown"


def quality_of(title):
    match = HIRES.search(title)
    if match:
        rate = match.group(2).replace(",", ".")
        return int(match.group(1)), {"44": "44.1", "88": "88.2", "176": "176.4"}.get(rate, rate)
    return (24, None) if BIT_24.search(title) else (None, None)


def score(album, item):
    """Returns {tier, score, quality, notes}. tier is good, maybe or no; no always gives a reason."""
    title, size = item["title"], item["size"]
    tokens = words(title)
    notes, reasons = [], []
    compilation = fold(album["artist"]) in ("various artists", "various", "va")

    rest = list(tokens)
    if not compilation:
        span = locate(rest, album["artist"])
        if span:
            del rest[span[0]:span[1]]
        else:
            reasons.append("Doesn't name the artist")
    span = next(filter(None, (locate(rest, form) for form in album_forms(album["title"]))), None)
    single = sequel = False
    if span:
        sequel = rest[span[1]:span[1] + 1] and rest[span[1]] in SEQUEL
        before = rest[span[0] - 1] if span[0] else ""
        named = any(words(t["title"]) == words(album["title"]) for t in album.get("tracks_list") or [])
        single = named and before.isdigit()
        del rest[span[0]:span[1]]
    else:
        reasons.append("Doesn't name this album")

    after_track = False
    for track in album.get("tracks_list") or []:
        found = find_span(rest, words(track["title"]))
        if found and len(words(track["title"])) >= 1:
            del rest[found[0]:found[1]]
            after_track = True
            break
    extra = [t for t in rest if t not in NOISE and not re.fullmatch(r"\d+|(19|20)\d\d", t)]

    kind = format_of(title, tokens, item.get("categories") or [])
    if kind == "lossy":
        reasons.append("Not FLAC")
    elif kind == "surround":
        reasons.append("Surround mix, not stereo")
    elif kind == "other":
        reasons.append("Not FLAC")

    seconds = album.get("seconds") or 0
    kbps = size * 8 / 1000 / seconds if size and seconds else None
    bits, rate = quality_of(title)
    if kbps is not None:
        low = HIRES_MIN_KBPS if bits == 24 else UNKNOWN_MIN_KBPS if kind == "unknown" else MIN_KBPS
        if kbps < low:
            reasons.append("Too small for the whole album")
        elif kbps > MAX_KBPS:
            reasons.append("Far bigger than the album, probably a box set")
    if item.get("password"):
        reasons.append("Password protected")

    points = 50
    if kind == "flac":
        points += 15
    if album.get("year") and album["year"] in tokens:
        points += 5
    if rate and float(rate) >= 176:
        points += 12
    elif rate and float(rate) >= 88:
        points += 10
    elif bits == 24:
        points += 6
    if not extra and not after_track:
        points += 8
    points -= min(len(extra), 4) * 3
    points += min(item.get("grabs") or 0, 50) // 10
    if after_track:
        points -= 4
        notes.append("Named after a track, but big enough to be the album")
    if single:
        points -= 15
        notes.append("May be one track from another album")
    if sequel:
        points -= 15
        notes.append("May be a different album with a similar name")
    if len(extra) >= 3:
        notes.append("Title has other words in it")
    if kind == "unknown":
        notes.append("Title doesn't say FLAC")
    label = f"{bits}/{rate}" if bits and rate else f"{bits}-bit" if bits else "FLAC" if kind == "flac" else ""
    if reasons:
        return {"tier": "no", "score": 0, "quality": label, "notes": reasons}
    return {"tier": "good" if points >= 70 else "maybe", "score": points, "quality": label, "notes": notes}


def similar(one, two):
    one, two = " ".join(words(one)), " ".join(words(two))
    if one == two:
        return 1.0
    plain = lambda text: re.sub(r"\b(19|20)\d\d\b|\b(digital )?remaster(ed)?\b|\bmono\b|\bstereo\b", "", text).strip()
    return SequenceMatcher(None, plain(one), plain(two)).ratio()


def fit(files, edition):
    """Pairs each track of an edition with the file that fits it best."""
    unused = list(files)
    pairs, missing = [], []
    tracks = edition["tracks"]
    ordered = sorted(files, key=lambda f: (f["disc"], f["track"] or 0))
    for index, track in enumerate(tracks):
        same_place = ordered[index] if len(ordered) == len(tracks) else None
        best, best_value = None, 0
        for item in unused:
            value = similar(track["title"], item["title"])
            close = track["seconds"] and abs(track["seconds"] - item["seconds"]) <= 3
            if close:
                value += 0.15
            if item is same_place and close:
                value = max(value, 0.8)
            if value > best_value:
                best, best_value = item, value
        if best and best_value >= 0.75:
            pairs.append((best, track))
            unused.remove(best)
        else:
            missing.append(track)
    if len(files) == len(tracks) and len(missing) <= max(1, len(tracks) // 10):
        # Same count and nearly all titles agree, so a track with a guest or subtitle in its name is that track.
        for track in list(missing):
            item = ordered[tracks.index(track)]
            if item in unused:
                pairs.append((item, track))
                unused.remove(item)
                missing.remove(track)
    return {"edition": edition, "pairs": pairs, "missing": missing, "extra": unused}


def best_edition(files, editions):
    """Prefers an exact fit, then whichever edition leaves the least unexplained."""
    fits = [fit(files, edition) for edition in editions]
    return min(fits, key=lambda f: (len(f["missing"]) + len(f["extra"]), len(f["extra"])))


VIDEO_END = re.compile(r"\.(vol\d+\+\d+\.)?par2$|\.vol\d+\+\d+$|\.(mpe?g|vob|avi|mkv|mp4|m4v|mov|wmv|ts|nfo|jpg|sfv|nzb)$", re.I)
VIDEO_CUT = re.compile(r"\s*[(\[{]|\s+-\s+|-(?:\d{3,4}p|x26[45]|h26[45]|dvd(?:rip)?|hdtv|hdrip|bdrip|bluray|web(?:rip|-?dl)?|ddc|svcd|vcd|proper)\b|\s\d{3,4}p\b", re.I)
VIDEO_LIVE = re.compile(r"\b(live|concert|unplugged|sessions?|festival|tour|totp|top of the pops|big weekend|glastonbury|"
                        r"performance|popsecret|bnn|acoustic)\b", re.I)
RESOLUTION = ((re.compile(r"2160p|\b4k\b|\buhd\b", re.I), 2160), (re.compile(r"1080[pi]", re.I), 1080), (re.compile(r"720p", re.I), 720),
              (re.compile(r"576[pi]|\bdvd\b|\.vob", re.I), 576), (re.compile(r"480[pi]|svcd|\bvcd\b", re.I), 480))
SCENE = re.compile(r"x26[45]-\d{4}-\w+$", re.I)
VIDEO_MIN = 10 * 10**6
VIDEO_FILM = 15 * 10**8


def video_song(artist, title):
    """The song name from a free-text title, or "" when it can't be told."""
    quoted = sorted(re.findall(r'"([^"]{3,})"', title), key=len, reverse=True)
    text = next((q for q in quoted if locate(words(q), artist)), title)
    while VIDEO_END.search(text.strip()):
        text = VIDEO_END.sub("", text.strip())
    text = text.replace("_", " ")
    if " " not in text:
        text = text.replace(".", " ")
    pattern = r"[\W_]*".join(re.escape(w) for w in re.findall(r"[^\W_]+", artist.casefold().replace("the ", "", 1)))
    found = re.search(pattern, text.casefold())
    if not found:
        return ""
    after = re.sub(r"^\s*(?:feat|ft)\.?\s.*?-", "", text[found.end():].lstrip(" -.:"), flags=re.I)
    song = " ".join(VIDEO_CUT.split(after)[0].strip(" -._").split())
    song = re.sub(r"-(CONVERT|READNFO|INTERNAL|REPACK|PROPER)$", "", song, flags=re.I)
    return song.title() if song.islower() else song


def score_video(artist, item):
    title, size = item["title"], item["size"]
    song = video_song(artist, title)
    resolution = next((value for pattern, value in RESOLUTION if pattern.search(title)), 0)
    live = bool(VIDEO_LIVE.search(title.replace("_", " ")))
    reasons, notes = [], []
    if not locate(words(title), artist):
        reasons.append("Doesn't name the artist")
    if not song:
        reasons.append("Can't tell which song it is")
    if size < VIDEO_MIN:
        reasons.append("Too small to be a video")
    if item.get("password"):
        reasons.append("Password protected")
    if live:
        notes.append("Live performance")
    if size > VIDEO_FILM:
        notes.append("Big enough to be a whole concert")
    points = resolution // 10 + (20 if SCENE.search(title) else 0) - (15 if live else 0)
    label = f"{resolution}p" if resolution > 576 else "DVD" if resolution == 576 else "SD"
    if reasons:
        return {"tier": "no", "score": 0, "quality": label, "song": song, "notes": reasons}
    return {"tier": "good" if resolution >= 720 and not live else "maybe", "score": points, "quality": label, "song": song, "notes": notes}


# ---------------------------------------------------------------- YouTube

YOUTUBE_WRONG = ("live", "cover", "remix", "lyrics", "lyric", "karaoke", "sped up", "slowed", "reaction", "8d",
                 "instrumental", "nightcore", "reverb", "tutorial", "lesson", "full album", "concert")


def youtube_fit(track_seconds, video_seconds):
    """good, close or off by length; "" when either length is unknown."""
    if not track_seconds or not video_seconds:
        return ""
    gap = abs(track_seconds - video_seconds)
    if gap <= max(4, track_seconds * 0.05):
        return "good"
    return "close" if gap <= max(10, track_seconds * 0.15) else "off"


def audio_only(channel):
    """YouTube's auto-generated "Artist - Topic" uploads are the album audio over a still of the cover."""
    return (channel or "").rstrip().endswith(" - Topic")


def score_youtube(track, artist, video):
    """Ranks a video for one track: the right length, the artist's own channel, the track's title,
    and none of the words that mean it's some other version."""
    title, channel = words(video["title"]), words(video.get("channel"))
    named = words(artist)
    fit = youtube_fit(track.get("seconds"), video["seconds"])
    gap = abs((track.get("seconds") or 0) - video["seconds"])
    points, notes = 100.0, []
    if track.get("seconds") and video["seconds"]:
        points -= min(60.0, gap / track["seconds"] * 200)
        if fit != "good":
            notes.append(f"{'Longer' if video['seconds'] > track['seconds'] else 'Shorter'} by {gap // 60}:{gap % 60:02d}")
    if audio_only(video.get("channel")) and find_span(channel, named) is not None:
        points += 40
        notes.append("Audio only, so no video is filed")
    elif channel and (channel == named or channel in (named + ["vevo"], named + ["official"], ["".join(named) + "vevo"])):
        points += 25
    if locate(title, artist) or locate(channel, artist):
        points += 10
    else:
        points -= 15
        notes.append("Doesn't name the artist")
    if find_span(title, words(track["title"])) is not None:
        points += 20
    else:
        points -= 30
        notes.append("Title doesn't match")
    wanted = " ".join(words(track["title"]))
    said = " ".join(title)
    for word in YOUTUBE_WRONG:
        if f" {word} " in f" {said} " and f" {word} " not in f" {wanted} ":
            points -= 40
            notes.append(word.capitalize())
            break
    if video.get("live"):
        points -= 100
        notes.append("A live stream")
    return {"fit": fit, "score": round(points, 1), "notes": notes}
