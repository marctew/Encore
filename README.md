# Encore

A self-hosted web app for finding albums and music videos on your Newznab indexers, downloading them with SABnzbd, and filing them into your music library.

It searches with several spellings, matches release titles on artist, album, size and format, and shows every result it leaves out with the reason why. Nothing reaches your library until it has been checked against a MusicBrainz track list and every track has been decoded.

- Search your library and MusicBrainz from one box
- Browse your Plex library and see which albums you're missing
- A Wanted list, and a queue you can drag to reorder
- Music videos filed so they appear in Plex
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
| Plex | Optional. Its address, then press **Connect Plex** at the top of the page |
| Music library | Where albums are filed, as `Artist/Album (Year)` |
| Import folder | Albums that need you to decide, such as ones you already own |
| Downloads | Where SABnzbd puts Encore's downloads |

Press **Save and test** to check everything connects.

- The account Encore runs as (`encore`) needs write access to all three folders.
- SABnzbd and Encore must see the Downloads folder at the same path.
- Plex can see your music at a different path, as long as the folder has the same name.

## Password

Encore is open to anyone on your network until you set a password in **Settings**. It runs on plain HTTP, so put it behind HTTPS before exposing it to the internet.

Behind a tunnel or proxy, set `ENCORE_TRUSTED_PROXY` to the proxy's IP address in the service file, so wrong-password locks apply per visitor and the sign-in cookie is HTTPS-only.

If you forget the password, delete `data/auth.json` in the install folder and restart Encore:

```bash
systemctl restart encore
```

## Checks

These run against fakes and throwaway folders, so they touch nothing real:

```bash
cd /opt/encore/app && ../venv/bin/python check_usenet.py && ../venv/bin/python check_auth.py
```

## Note

Encore is a search and filing tool and includes no content. You're responsible for making sure what you download is legal where you are, and for the terms of the services you use with it.
