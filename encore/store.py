"""Everything Encore keeps between runs, so MusicBrainz is only asked once per artist or album.

Nothing here expires by itself: a Refresh button on the page is what fetches newer data.
"""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (id TEXT PRIMARY KEY, name TEXT, info TEXT, photo TEXT DEFAULT '',
    photo_at REAL DEFAULT 0, albums_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS albums (id TEXT PRIMARY KEY, artist_id TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS albums_by_artist ON albums (artist_id);
CREATE TABLE IF NOT EXISTS details (id TEXT PRIMARY KEY, data TEXT, saved_at REAL);
CREATE TABLE IF NOT EXISTS searches (kind TEXT, query TEXT, ids TEXT, saved_at REAL, PRIMARY KEY (kind, query));
CREATE TABLE IF NOT EXISTS plex_artists (rating TEXT PRIMARY KEY, name TEXT, mbid TEXT, thumb TEXT);
CREATE TABLE IF NOT EXISTS plex_albums (rating TEXT PRIMARY KEY, artist_rating TEXT, artist TEXT, title TEXT,
    year INTEGER, thumb TEXT);
CREATE TABLE IF NOT EXISTS wanted (album_id TEXT PRIMARY KEY, artist_id TEXT, data TEXT, added REAL,
    searched_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS facts (name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS plex_links (rating TEXT PRIMARY KEY, mbid TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS youtube_drafts (album_id TEXT PRIMARY KEY, edition TEXT, links TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS youtube_videos (id TEXT PRIMARY KEY, data TEXT, saved_at REAL);
"""


# Plex artists with their MusicBrainz id: Plex's own, or the one Encore found for an artist Plex hasn't matched.
ARTISTS = ("(SELECT a.rating, a.name, a.thumb, COALESCE(NULLIF(a.mbid, ''), l.mbid, '') AS mbid "
           "FROM plex_artists a LEFT JOIN plex_links l ON l.rating = a.rating AND l.name = a.name)")


class Store:
    """One connection per thread (the page's threads, the queue and the background jobs), in WAL
    mode, so reads never wait on a write and a transaction always stays on its own connection."""

    def __init__(self, path):
        self.path = path
        self.local = threading.local()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    @property
    def db(self):
        if not hasattr(self.local, "db"):
            db = sqlite3.connect(self.path, isolation_level=None, timeout=15)
            db.row_factory = sqlite3.Row
            self.local.db = db
        return self.local.db

    def rows(self, sql, *values):
        return self.db.execute(sql, values).fetchall()

    def one(self, sql, *values):
        return self.db.execute(sql, values).fetchone()

    def save_artist(self, info):
        self.db.execute("INSERT INTO artists (id, name, info) VALUES (?, ?, ?) "
                        "ON CONFLICT (id) DO UPDATE SET name = excluded.name, info = excluded.info",
                        (info["id"], info["name"], json.dumps(info)))

    def artist(self, artist_id):
        row = self.one("SELECT * FROM artists WHERE id = ?", artist_id)
        return {**json.loads(row["info"]), "photo": row["photo"], "photo_at": row["photo_at"],
                "albums_at": row["albums_at"]} if row else None

    def set_photo(self, artist_id, value):
        self.db.execute("UPDATE artists SET photo = ?, photo_at = ? WHERE id = ?", (value, time.time(), artist_id))

    def clear_photo(self, artist_id):
        self.db.execute("UPDATE artists SET photo = '', photo_at = 0 WHERE id = ?", (artist_id,))

    def save_album(self, album):
        self.db.execute("INSERT OR REPLACE INTO albums (id, artist_id, data) VALUES (?, ?, ?)",
                        (album["id"], album.get("artist_id") or "", json.dumps(album)))

    def save_albums(self, artist_id, albums):
        self.db.execute("BEGIN")
        self.db.execute("DELETE FROM albums WHERE artist_id = ?", (artist_id,))
        for album in albums:
            self.save_album(album)
        self.db.execute("UPDATE artists SET albums_at = ? WHERE id = ?", (time.time(), artist_id))
        self.db.execute("COMMIT")

    def album(self, album_id):
        row = self.one("SELECT data FROM albums WHERE id = ?", album_id)
        return json.loads(row["data"]) if row else None

    def albums_of(self, artist_id):
        return [json.loads(r["data"]) for r in self.rows("SELECT data FROM albums WHERE artist_id = ?", artist_id)]

    def save_details(self, album_id, details):
        self.db.execute("INSERT OR REPLACE INTO details (id, data, saved_at) VALUES (?, ?, ?)",
                        (album_id, json.dumps(details), time.time()))

    def details(self, album_id):
        row = self.one("SELECT data, saved_at FROM details WHERE id = ?", album_id)
        return {**json.loads(row["data"]), "saved_at": row["saved_at"]} if row else None

    def has_details(self, album_id):
        return bool(self.one("SELECT 1 FROM details WHERE id = ?", album_id))

    def save_search(self, kind, query, ids):
        self.db.execute("INSERT OR REPLACE INTO searches (kind, query, ids, saved_at) VALUES (?, ?, ?, ?)",
                        (kind, query, json.dumps(ids), time.time()))

    def search(self, kind, query):
        row = self.one("SELECT ids, saved_at FROM searches WHERE kind = ? AND query = ?", kind, query)
        return (json.loads(row["ids"]), row["saved_at"]) if row else (None, 0)

    def replace_plex(self, artists, albums):
        self.db.execute("BEGIN")
        self.db.execute("DELETE FROM plex_artists")
        self.db.execute("DELETE FROM plex_albums")
        self.db.executemany("INSERT INTO plex_artists VALUES (:rating, :name, :mbid, :thumb)", artists)
        self.db.executemany("INSERT INTO plex_albums VALUES (:rating, :artist_rating, :artist, :title, :year, :thumb)", albums)
        self.db.execute("INSERT OR REPLACE INTO facts VALUES ('plex_at', ?)", (str(time.time()),))
        self.db.execute("COMMIT")

    def plex_at(self):
        row = self.one("SELECT value FROM facts WHERE name = 'plex_at'")
        return float(row["value"]) if row else 0

    def plex_artists(self):
        return [dict(r) for r in self.rows(
            f"SELECT a.rating, a.name, a.mbid, a.thumb, COUNT(b.rating) AS albums FROM {ARTISTS} a "
            "LEFT JOIN plex_albums b ON b.artist_rating = a.rating GROUP BY a.rating ORDER BY a.name COLLATE NOCASE")]

    def plex_albums(self):
        return [dict(r) for r in self.rows("SELECT a.*, p.mbid FROM plex_albums a "
                                           f"LEFT JOIN {ARTISTS} p ON p.rating = a.artist_rating")]

    def plex_artist(self, mbid):
        row = self.one(f"SELECT * FROM {ARTISTS} WHERE mbid = ?", mbid)
        return dict(row) if row else None

    def plex_thumb(self, rating):
        row = self.one("SELECT thumb FROM plex_artists WHERE rating = ? "
                       "UNION SELECT thumb FROM plex_albums WHERE rating = ?", rating, rating)
        return row["thumb"] if row else None

    def want(self, album):
        self.db.execute("INSERT OR IGNORE INTO wanted (album_id, artist_id, data, added) VALUES (?, ?, ?, ?)",
                        (album["id"], album.get("artist_id") or "", json.dumps(album), time.time()))

    def unwant(self, album_id):
        self.db.execute("DELETE FROM wanted WHERE album_id = ?", (album_id,))

    def wanted(self):
        return [{**json.loads(r["data"]), "added": r["added"], "searched_at": r["searched_at"]}
                for r in self.rows("SELECT * FROM wanted ORDER BY added DESC")]

    def wanted_ids(self):
        return {r["album_id"] for r in self.rows("SELECT album_id FROM wanted")}

    def youtube_draft(self, album_id):
        """The links chosen so far for an album: {"edition": id, "links": {"disc-number": video id}}."""
        row = self.one("SELECT edition, links FROM youtube_drafts WHERE album_id = ?", album_id)
        return {"edition": row["edition"], "links": json.loads(row["links"])} if row else {"edition": "", "links": {}}

    def save_youtube_draft(self, album_id, edition, links):
        self.db.execute("INSERT OR REPLACE INTO youtube_drafts (album_id, edition, links, updated) VALUES (?, ?, ?, ?)",
                        (album_id, edition, json.dumps(links), time.time()))

    def youtube_video(self, video_id):
        row = self.one("SELECT data, saved_at FROM youtube_videos WHERE id = ?", video_id)
        return {"video": json.loads(row["data"]), "saved_at": row["saved_at"]} if row else None

    def save_youtube_video(self, video):
        self.db.execute("INSERT OR REPLACE INTO youtube_videos (id, data, saved_at) VALUES (?, ?, ?)",
                        (video["id"], json.dumps(video), time.time()))

    def plex_artist_by_rating(self, rating):
        row = self.one(f"SELECT * FROM {ARTISTS} WHERE rating = ?", rating)
        return dict(row) if row else None

    def link_plex(self, rating, mbid, name):
        """Remembers the MusicBrainz artist for one Plex hasn't matched. Tied to the name too, so it
        lapses if Plex ever reuses the rating key for someone else."""
        self.db.execute("INSERT OR REPLACE INTO plex_links (rating, mbid, name) VALUES (?, ?, ?)", (rating, mbid, name))
