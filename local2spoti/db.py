from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

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
    await conn.commit()
