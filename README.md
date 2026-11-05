# Encore

A self-hosted web app for finding albums and music videos on your Newznab indexers, downloading them with SABnzbd, and filing them into your music library.

It searches with several spellings, matches release titles on artist, album, size and format, and shows every result it leaves out with the reason why. Nothing reaches your library until it has been checked against a MusicBrainz track list and every track has been decoded.

- Search your library and MusicBrainz from one box
- Browse your Plex library and see which albums you're missing
- A Wanted list, and a queue you can drag to reorder
- Music videos filed so they appear in Plex
- Build an album from YouTube links when Usenet doesn't have it
- Optional password, and works on phones

## Install

On Debian or Ubuntu, as root:

```bash
apt-get update && apt-get install -y curl && curl -fsSL https://raw.githubusercontent.com/marctew/Encore/main/install.sh | bash
```

Or from an account that uses sudo:

```bash
sudo bash -c "apt-get update && apt-get install -y curl && curl -fsSL https://raw.githubusercontent.com/marctew/Encore/main/install.sh | bash"
```

That installs everything Encore needs and starts it on port 3346. Run it again to update.

To change the defaults, set `ENCORE_PORT`, `ENCORE_DIR` or `ENCORE_USER` for the final `bash`.

## Set up

Open `http://your-server:3346`, go to **Settings**, and add:

| Setting | What it's for |
|---|---|
| Indexers | The address and API key of each Newznab indexer |
| SABnzbd | Its address and API key. Encore creates its own `encore` category |
| Plex | Optional. Its address, then press **Connect Plex** in the banner that appears |
| Music library | Where albums are filed, as `Artist/Album (Year)` |
| Import folder | Albums that need you to decide, such as ones you already own |
| Downloads | Where SABnzbd puts Encore's downloads |

Press **Save and test** to check everything connects.

- The account Encore runs as (`encore`) needs write access to all three folders.
- SABnzbd and Encore must see the Downloads folder at the same path.
- Plex can see your music at a different path, as long as the folder has the same name.

## YouTube

When Usenet doesn't have an album, open it and press **Build from YouTube**. Paste a link for each track, or press **Find** (or **Fill all**) to have Encore search YouTube and pick the closest match by length, channel and title. The links are saved as you go.

Each video is downloaded once, at up to 1080p. Its sound is copied out without re-encoding to make the album: AAC at about 128 kbps, so lossy. The album is tagged from MusicBrainz, given its cover, and filed like any other, and the queue and Library show it as `YouTube AAC`. Each video goes in the artist's folder as `Song-video.mp4`, where Plex shows it as a music video. Uploads from YouTube's auto-generated "Artist - Topic" channels are the album audio over a still, so no video is filed for those.

YouTube changes often, and old versions of yt-dlp stop working. `scripts/update.sh` upgrades yt-dlp every time it runs, and **Settings** has an **Update yt-dlp** button.

## Password

Encore is open to anyone on your network until you set a password in **Settings**. It runs on plain HTTP, so put it behind HTTPS before exposing it to the internet.

Behind a tunnel or proxy, set `ENCORE_TRUSTED_PROXY` to the proxy's IP address in the service file, so wrong-password locks apply per visitor and the sign-in cookie is HTTPS-only.

If you forget the password, delete `/var/lib/encore/auth.json` and restart Encore:

```bash
systemctl restart encore
```

## Checks

These run against fakes and throwaway folders, so they touch nothing real:

```bash
cd /opt/encore && venv/bin/python tools/check_usenet.py && venv/bin/python tools/check_auth.py && venv/bin/python tools/check_youtube.py
```

## Updating and running

`sudo /opt/encore/scripts/update.sh --pull` pulls, installs anything new, checks the code compiles and restarts, waiting if an album is being checked or filed. Running `install.sh` again does the same.

Encore is a Flask app served by gunicorn (`wsgi.py`, one worker), as the `encore` systemd service. Its data (settings, the queue, the database, photos) is in `/var/lib/encore`; set `ENCORE_DATA` to move it. `/healthz` answers without a sign-in, for uptime monitors. For development, `python -m encore` runs it on its own.

The look comes from [Design Uniformity](https://github.com/marctew/design-uniformity), vendored in `encore/static/design`, `encore/templates/partials` and `encore/du_flask.py`. Those files are replaced by its updater, so change them there, not here. Settings has a **Design system** card for it, which only works once the updater is installed on the host.

## Note

Encore is a search and filing tool and includes no content. You're responsible for making sure what you download is legal where you are, and for the terms of the services you use with it, including YouTube's.
