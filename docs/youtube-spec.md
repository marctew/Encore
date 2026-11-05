# YouTube albums: spec

Build an album from YouTube links when Usenet doesn't have it. One link per track: Encore downloads each video once, takes the audio out of it without re-encoding to make the album, and files the videos as the artist's music videos.

Status: agreed design, not built. Read `README.md`, `CLAUDE.md`, `docs/design/APP-STANDARD.md` and `docs/design/DESIGN-SPEC.md` first. The rules in `CLAUDE.md` (X-Encore header, password guard, data in `/var/lib/encore`, never editing the vendored design files) apply to everything here.

## Decisions

| Question | Decision |
|---|---|
| Audio format | M4A (AAC), copied straight out of the video's audio stream. No re-encode, no FLAC. |
| Where audio goes | The library, `Artist/Album (Year)`, the same as a Usenet album, with every existing Hold rule (already owned, name clash and so on). The queue and Library show the quality as `YouTube AAC 128`. |
| Where video goes | The artist folder as `Song-video.mp4`, through `filing.to_artist`, the same as Usenet music videos. Plex shows these as the artist's music videos. |
| Finding links | Paste a URL per track. Each track also has **Find**, and there's a **Fill all**, which search YouTube and suggest the closest match. |
| Not in v1 | One full-album video split by chapters, playlists, cookies for age-restricted videos. |

## Dependencies

- `yt-dlp[default]` in `requirements.txt`, unpinned. Run it as a **subprocess** (`venv/bin/yt-dlp`), never imported: an upgrade then takes effect without a restart, and a crash in it can't take the worker down.
- YouTube support in yt-dlp now needs a JavaScript runtime (Deno). Check the current yt-dlp README when building for the supported way to provide it. The preferred route is the `deno` package from PyPI in the venv, so `install.sh` needs no new apt source. Pass `--js-runtimes deno:<path>` explicitly if yt-dlp doesn't find it on its own.
- `ffmpeg` and `ffprobe` are already installed.
- `scripts/update.sh`: after the requirements step, always run `venv/bin/pip install -q -U "yt-dlp[default]"`, whether or not `requirements.txt` changed. YouTube breaks older yt-dlp versions every few weeks.
- `install.sh` gets the same packages through `requirements.txt`. Nothing new from apt.

## Data

New tables in `store.py` (`CREATE TABLE IF NOT EXISTS`, so existing databases pick them up):

```sql
CREATE TABLE IF NOT EXISTS youtube_drafts (album_id TEXT PRIMARY KEY, edition TEXT, links TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS youtube_videos (id TEXT PRIMARY KEY, data TEXT, saved_at REAL);
```

- `links` is JSON: `{"<disc>-<number>": "<video id>"}`. Store only the 11-character video ID, never the URL as pasted.
- `youtube_videos` caches lookups: `{id, title, channel, seconds, thumb, uploader_id}`. Cached lookups are used for 7 days.

## Security: URLs

Never pass a string the user typed to yt-dlp. In `sources.py`, add a `youtube_id(text)` function:

- Accept only `youtube.com`, `www.youtube.com`, `m.youtube.com`, `music.youtube.com` and `youtu.be`. Take the ID from `watch?v=`, `/shorts/`, `/live/`, `/embed/`, or the `youtu.be/` path.
- The ID must match `^[A-Za-z0-9_-]{11}$`. Anything else is a `Refusal(400, "That isn't a YouTube video link")`.
- Always call yt-dlp with the rebuilt `https://www.youtube.com/watch?v=<id>`, after a `--` so nothing can be read as an option, with `--no-playlist`, and with an argument list (never `shell=True`).
- Search queries go in as one argument, `ytsearch8:<artist> <title>`, also after `--`.

## yt-dlp wrapper (`sources.py`, class `YouTube`)

- `lookup(video_id)` runs `yt-dlp -J --skip-download --no-playlist -- URL` and returns the cached shape above. Errors map to readable `SourceError`s:
  - "Video unavailable" → "That video has been removed or made private"
  - "Sign in to confirm your age" → "That video is age-restricted, which Encore can't download yet"
  - "Sign in to confirm you're not a bot" → "YouTube is blocking downloads from this server for now. Try again later"
  - anything else → "YouTube didn't answer. Try again shortly", with the detail in the log.
- `search(artist, title)` runs `yt-dlp -J --flat-playlist -- "ytsearch8:{artist} {title}"` and returns the entries in the same shape.
- `download(video_id, folder, progress)` downloads into `folder` and returns the video path. Format: `-S "res:1080,vcodec:h264,acodec:m4a" --merge-output-format mp4`, `-o "<folder>/%(id)s.%(ext)s"`, `--newline --progress-template "download:%(progress._percent_str)s"`, `--retries 5 --sleep-requests 1`. Read stdout line by line to report progress. Keep the `Popen` handle so a cancel can kill it.
- `version()` returns `yt-dlp --version`, for Settings.
- One lock around every yt-dlp call, so lookups from the page and the queue's download never hit YouTube at the same time.
- Timeouts: 30 s for lookup and search. Downloads have none, but a cancel kills them.

## Choosing a match (`match.py`, `score_youtube(track, album, entry)`)

Returns `{score, fit, notes}`, where `fit` is `good`, `close` or `off`:

- Length: within 5% (and at least ±4 s) is `good`, within 15% is `close`, beyond that is `off`.
- Bonus when the channel is the artist, `<artist> - Topic`, or `<artist>VEVO` (compare with `norm`). Topic-channel uploads are the album audio, so they get the biggest bonus.
- Bonus when the video title contains the track title (`norm`'d).
- Penalty for `live`, `cover`, `remix`, `lyrics`, `karaoke`, `sped up`, `slowed`, `reaction`, `8d`, `instrumental`, unless the track title has that word itself.
- **Find** returns the top 5 by score. **Fill all** takes the top one for every track that's empty.

## API (`web.py`)

Every POST needs `X-Encore: 1`, as the guard already enforces.

| Route | Does |
|---|---|
| `GET /api/album/<id>/youtube` | `{edition, editions: [{id, label, tracks}], links: {key: video}}`, where `video` is the cached lookup and its `score_youtube` result. The default edition is the album's main one (`tracks_list`). |
| `POST /api/album/<id>/youtube` | `{edition, links: {key: url-or-id-or-""}}` saves the draft. Each value goes through `youtube_id`, and a blank clears that track. Returns the same shape as GET. |
| `POST /api/youtube/lookup` | `{url}` → `{video, fit, notes}` for one track. Also takes `album_id` and `key`, so it can score against that track. |
| `POST /api/album/<id>/youtube/find` | `{key}` → `{results: [...]}`, the top 5 scored. |
| `POST /api/queue/youtube` | `{album_id, partial}` queues the saved draft. Refuse with 409 if no tracks are linked, if some tracks are blank and `partial` isn't set, if this album is already active in the queue, or if the folders aren't set. SABnzbd isn't needed. |
| `POST /api/settings/youtube/update` | Runs `pip install -U "yt-dlp[default]"` in a thread and returns the new version. |

## Queue job

Use the existing `Queue`, one job at a time. Add `add_youtube(album, edition, tracks)` next to `add_video`:

```python
{**album basics, "kind": "youtube", "album_id": album["id"], "id": ..., "state": "queued",
 "source": {"type": "youtube", "edition": edition_id,
            "tracks": [{"key": "1-3", "disc": 1, "number": 3, "title": "...", "video": "<id>", "seconds": 241}]},
 "partial": bool, "fix_artist": False}
```

The job's `title`, `artist`, `year` and `cover` come from the album, as in `Queue.add`. `public(job)` hides nothing new.

`process_job` gets a `kind == "youtube"` branch:

1. **Space.** The estimate is the total seconds × 1.2 MB, as a 1080p allowance. Fail with "Not enough free space on the share for this album" if free space is under 2 × estimate + `MIN_FREE`.
2. **Download.** Folder: `DOWNLOADS/encore-youtube/<job id>/`. Set `job["storage"]` to it, so `forget` and `inside_downloads` clean it up. For each track, skip it if `<id>.mp4` is already there and probes clean (so a retry or restart resumes), otherwise download it. Run `check_cancel` between tracks. Progress: `percent` = overall %, `step` = "Track 4 of 12". Any track that fails ends the job with `"Track 4 (“Title”): <reason>. Change its link and retry."`, and sets `kept=True` so the retry reuses the tracks already downloaded.
3. **Verify** (state `verifying`, message "Checking every track"):
   - Each video: `probe` shows a video stream and an audio stream, and `ffmpeg -v error ... -f null -` reads through cleanly (the `prepare_video` check, with audio included).
   - Its duration is within 2 s of the length the lookup gave (catches a cut-off download).
   - Extract the audio: `ffmpeg -nostdin -v error -i <id>.mp4 -map 0:a:0 -vn -c:a copy -movflags +faststart album/<NN>.m4a`. If the stream isn't AAC (rare, but possible with fallbacks), re-encode with `-c:a aac -b:a 192k` and note it.
   - `decode_ok` on every `.m4a` (it already uses ffmpeg for non-FLAC).
   - Lengths against MusicBrainz are **not** a failure, because the user chose these links. Any track outside the `close` band adds to `job["edition_note"]`, e.g. "3 tracks differ in length from the album".
4. **Tag.** Build `pairs` in the shape `write_tags` expects (`[({"path": m4a}, edition_track)]`) and call `write_tags(pairs, album, edition, fix_artist=True)`. Easy MP4 tags cover album, albumartist, title, artist, tracknumber, discnumber and date. Fetch the front cover from Cover Art Archive (`cover(album_id, 1200)`). If it arrives, save it as `album/cover.jpg` and embed it with mutagen `MP4Cover`. A missing cover isn't a failure.
5. **File audio** (state `filing`): `file_album(album_dir, job["id"], self.library, hold)`, where `hold` is set when `partial` left tracks out ("Missing …" goes in `partial_note` instead, as Usenet does). `job["quality"] = "YouTube AAC <kbps>"`, from the lowest bitrate.
6. **File videos.** For each track: `filing.to_artist(video, album["artist"], track["title"], MUSIC, job_id)`. Add a `skip_existing=True` argument so an existing `Song-video.mp4` is skipped rather than failing. Do this even when the audio went to the import folder: the videos are separate from the album. Collect them in `job["videos"]` (filed and skipped counts) and add "8 videos filed, 2 already there" to the message.
7. **Plex.** `finish` → `announce`. For `youtube` jobs, scan the artist folder (`folder.split("/")[0]`), which covers both the album and the videos, then `plex.confirm` the album as usual. If the audio went to import, scan only for the videos.

Restart behaviour: `Queue.load` already puts `downloading/verifying/filing` back to `queued`, and step 2 resumes from the files already on disk.

Cancel: `check_cancel` between tracks, plus a `cancel` flag the download loop watches, which kills the yt-dlp process.

## Page (`encore.js`, `encore.css`)

- **Album dialog** (`renderDetail`): add `btn('Build from YouTube', …)` to `.row.actions`, next to Want and Refresh tracks. It switches the dialog body to the builder (`detail.view = 'youtube'`), with a **Back** that returns to Downloads and Tracks. Give it its own URL, as the other views have, e.g. `?album=<id>&view=youtube`, if the router supports it.
- **Builder layout:** a header row with an edition `<select>` (only when there's more than one edition; label like "12 tracks · 1997 · Official"), a count "9 of 12 linked", **Fill all** (ghost) and **Queue** (primary). Below that, one row per track, grouped by disc as `trackRows` does:
  - Left: number, title, length (the `trackRows` markup).
  - Right: a URL input (placeholder "Paste a YouTube link"), a **Find** icon button and a clear button. Once linked, a compact preview: 64×36 thumbnail, video title (one line, truncated), channel · length, and a fit badge: `badge-success` "Good fit", `badge-warning` "Length differs", `badge-danger` "Very different".
  - **Find** opens an inline list of the 5 results (thumbnail, title, channel, length, badge), and clicking one fills the row.
  - On phones the two columns stack: track above, link below.
- **Saving:** look up on paste, Enter or blur; save the draft (debounced 600 ms) after any change. Show errors next to the row in the same style as the existing `.why`.
- **Fill all:** calls `find` for each empty track one at a time (never in parallel), filling rows as each answers, with a Stop button while it runs.
- **Queue:** if some tracks are blank, use `duConfirm` with "3 tracks have no link. Queue the other 9?" and the button **Queue 9 tracks**. If any linked track is `off`, use `duConfirm` with "2 videos are very different in length from the album. Queue anyway?" Afterwards, switch back to the album view, where the Downloads buttons read "In the queue".
- **Queue page:** youtube jobs show a `badge-muted` "YouTube" next to the title, and `step` ("Track 4 of 12") under the progress bar. Retry on a failed youtube job re-queues it with the current draft, so a changed link is picked up.
- **Settings:** a **YouTube** card showing the yt-dlp version, an **Update yt-dlp** button, and a line explaining that YouTube audio is AAC and goes into the library like any album.
- Use only design-system classes and components. Anything new goes in `encore/static/encore.css`.

## Tests

Add `tools/check_youtube.py` in the style of `check_usenet.py`: PASS lines, a non-zero exit on any failure, throwaway folders and a fake `yt-dlp`.

- The fake is a Python script put first on `PATH` (or set via an `ENCORE_YTDLP` env var the wrapper honours). It answers `-J` with canned JSON, `--version`, search, and downloads by generating an mp4 with `ffmpeg -f lavfi` (testsrc plus sine, AAC audio, the requested length).
- `youtube_id`: accepts every supported URL form; rejects other hosts, `--exec`, playlist-only links, and IDs of the wrong length.
- Draft save and load; a blank clears a track.
- Happy path: a 3-track album is filed to the library with correct tags and file names, `cover.jpg` is present, quality reads `YouTube AAC …`, and three `-video.mp4` files are in the artist folder.
- An existing video is skipped, not a failure.
- Partial: with one blank track and `partial` set, the album is filed with a "Missing …" note. Without `partial`, the request gets a 409.
- A failed track download sets the job to failed with the track named. After a retry, the tracks already downloaded aren't downloaded again (count the fake's calls).
- Cancel during a download kills the fake and the job reads Cancelled.
- An album already owned goes to import, and its videos are still filed.
- Extend `tools/check_auth.py`: the new POST routes refuse without `X-Encore: 1` and without the session cookie when a password is set.

Run all three checks with the venv's Python. Try the page on a throwaway copy of the data on a spare port.

## README

- Add "Build albums from YouTube links when Usenet doesn't have them" to the feature list.
- Add a **YouTube** section: audio is AAC (lossy) and filed like any album; videos go to the artist folder; yt-dlp updates with every `update.sh`, or from Settings.
- Extend the **Note** paragraph to cover YouTube's terms.
