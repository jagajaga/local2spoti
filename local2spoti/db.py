from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA cache_size = -65536",
    "PRAGMA mmap_size = 268435456",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_run (
  id              INTEGER PRIMARY KEY,
  root_path       TEXT NOT NULL,
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  status          TEXT NOT NULL,
  threshold       TEXT NOT NULL,
  total_files     INTEGER,
  matched_count   INTEGER,
  review_count    INTEGER,
  unmatched_count INTEGER,
  error_message   TEXT
);

CREATE TABLE IF NOT EXISTS local_file (
  id               INTEGER PRIMARY KEY,
  path             TEXT NOT NULL UNIQUE,
  mtime            INTEGER NOT NULL,
  size             INTEGER NOT NULL,
  format           TEXT NOT NULL,
  duration_ms      INTEGER,
  artist           TEXT,
  title            TEXT,
  album            TEXT,
  track_number     INTEGER,
  isrc             TEXT,            -- International Standard Recording Code from file tags; lets us hit Spotify with q=isrc:XXX for a deterministic match
  metadata_source  TEXT,
  status           TEXT NOT NULL,
  spotify_track_id TEXT,
  match_confidence REAL,
  match_method     TEXT,
  first_seen_at    TEXT NOT NULL,
  last_scanned_at  TEXT,
  last_error       TEXT,
  last_run_id      INTEGER REFERENCES scan_run(id)
);
CREATE INDEX IF NOT EXISTS idx_local_file_status ON local_file(status);
CREATE INDEX IF NOT EXISTS idx_local_file_run    ON local_file(last_run_id);
CREATE INDEX IF NOT EXISTS idx_local_file_artist ON local_file(artist);

CREATE TABLE IF NOT EXISTS match_candidate (
  id                  INTEGER PRIMARY KEY,
  local_file_id       INTEGER NOT NULL REFERENCES local_file(id) ON DELETE CASCADE,
  spotify_track_id    TEXT NOT NULL,
  spotify_artist      TEXT NOT NULL,
  spotify_title       TEXT NOT NULL,
  spotify_album       TEXT,
  spotify_duration_ms INTEGER,
  artist_similarity   REAL NOT NULL,
  title_similarity    REAL NOT NULL,
  duration_delta_ms   INTEGER,
  confidence          REAL NOT NULL,
  rank                INTEGER NOT NULL,
  fetched_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_match_candidate_file ON match_candidate(local_file_id, rank);

CREATE TABLE IF NOT EXISTS playlist (
  id                  INTEGER PRIMARY KEY,
  spotify_playlist_id TEXT NOT NULL UNIQUE,
  name                TEXT NOT NULL,
  chunk_index         INTEGER NOT NULL,
  alpha_range         TEXT,
  created_at          TEXT NOT NULL,
  track_count         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlist_track (
  playlist_id      INTEGER NOT NULL REFERENCES playlist(id) ON DELETE CASCADE,
  local_file_id    INTEGER NOT NULL REFERENCES local_file(id) ON DELETE CASCADE,
  spotify_track_id TEXT NOT NULL,
  added_at         TEXT NOT NULL,
  PRIMARY KEY (playlist_id, local_file_id)
);

CREATE TABLE IF NOT EXISTS auth_token (
  key           TEXT PRIMARY KEY,
  access_token  TEXT NOT NULL,
  refresh_token TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  scope         TEXT NOT NULL,
  user_id       TEXT
);

CREATE TABLE IF NOT EXISTS setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Persisted Spotify artist catalogs. First time we look up an artist we
-- pay the search + albums + albums-batch cost and store the resulting
-- track list here keyed by normalized name. Subsequent matches against
-- the same artist hit this cache instead of the Spotify API — zero
-- /search pressure on re-scans of the same library.
--
-- Cache miss when row is absent OR expires_at < now.
-- Negative results (artist not found on Spotify) get a row with
-- spotify_artist_id IS NULL and a shorter TTL so we re-probe sooner.
CREATE TABLE IF NOT EXISTS artist_catalog (
  artist_name_normalized TEXT PRIMARY KEY,
  spotify_artist_id      TEXT,           -- NULL if Spotify had no match for this name
  spotify_artist_name    TEXT,           -- canonical name as Spotify returned it
  tracks_json            TEXT,           -- orjson-encoded list of {id, name, album, duration_ms, artists}
  fetched_at             TEXT NOT NULL,
  expires_at             TEXT NOT NULL
);
"""


@asynccontextmanager
async def connect(path: Path) -> AsyncIterator[aiosqlite.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    try:
        for pragma in PRAGMAS:
            await conn.execute(pragma)
        yield conn
    finally:
        await conn.close()


async def init_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA)
    # Lightweight migration: add columns introduced after the first
    # release. CREATE TABLE IF NOT EXISTS is a no-op on existing DBs, so
    # new columns won't appear without an explicit ALTER. We probe with
    # PRAGMA table_info and ALTER if missing — idempotent and cheap.
    cur = await conn.execute("PRAGMA table_info(local_file)")
    columns = {row[1] for row in await cur.fetchall()}
    if "isrc" not in columns:
        await conn.execute("ALTER TABLE local_file ADD COLUMN isrc TEXT")
    await conn.commit()
