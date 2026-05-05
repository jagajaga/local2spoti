"""SQLite-backed cache of Spotify artist catalogs.

The match path used to refetch every artist's full discography on every
run — a Beach Boys file scanned today and another scanned tomorrow paid
the same artist-search + albums-list + albums-batch cost twice. This
module turns that into a one-time-per-artist cost (per TTL window) and
makes re-scans of an unchanged library effectively free of /v1/search
pressure.

Cache strategy:
  - Keyed by normalized artist name (so 'Beach Boys' and 'beach punk'
    are different rows; spelling variations from bad tags are not
    coalesced — by design, since matching against the wrong artist's
    catalog is worse than a cache miss).
  - 30-day TTL on positive hits (Spotify catalogs evolve as artists
    release new music; 30 days bounds staleness without invalidating
    too aggressively).
  - 1-day TTL on negative hits (artist not found) — short so we
    re-probe in case Spotify added the artist or our tag was bad.
  - On expiry we don't auto-evict; we just refresh on next read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
import orjson

from .normalize import normalize_artist

POSITIVE_TTL = timedelta(days=30)
NEGATIVE_TTL = timedelta(days=1)


@dataclass(slots=True)
class CachedCatalog:
    """A catalog row read from the cache (positive or negative)."""

    artist_name_normalized: str
    spotify_artist_id: str | None
    spotify_artist_name: str | None
    tracks: list[dict[str, Any]]
    fetched_at: datetime
    expires_at: datetime

    @property
    def is_positive(self) -> bool:
        return self.spotify_artist_id is not None

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


async def get(
    conn: aiosqlite.Connection, artist_name: str,
) -> CachedCatalog | None:
    """Return a cached catalog for `artist_name`, or None if not cached
    OR if the cached entry has expired (caller should re-fetch)."""
    norm = normalize_artist(artist_name)
    cur = await conn.execute(
        """SELECT artist_name_normalized, spotify_artist_id, spotify_artist_name,
                  tracks_json, fetched_at, expires_at
           FROM artist_catalog WHERE artist_name_normalized=?""",
        (norm,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    expires_at = datetime.fromisoformat(row[5])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if datetime.now(UTC) >= expires_at:
        return None  # expired — treat as cache miss; caller will re-fetch
    fetched_at = datetime.fromisoformat(row[4])
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    tracks = orjson.loads(row[3]) if row[3] else []
    return CachedCatalog(
        artist_name_normalized=row[0],
        spotify_artist_id=row[1],
        spotify_artist_name=row[2],
        tracks=tracks,
        fetched_at=fetched_at,
        expires_at=expires_at,
    )


async def put(
    conn: aiosqlite.Connection,
    artist_name: str,
    *,
    spotify_artist_id: str | None,
    spotify_artist_name: str | None,
    tracks: list[dict[str, Any]],
    now: datetime | None = None,
) -> None:
    """Cache (or refresh) a catalog. Pass spotify_artist_id=None for a
    negative-result cache entry (artist not found on Spotify)."""
    if now is None:
        now = datetime.now(UTC)
    ttl = POSITIVE_TTL if spotify_artist_id is not None else NEGATIVE_TTL
    expires_at = now + ttl
    tracks_json = orjson.dumps(tracks).decode() if tracks else "[]"
    await conn.execute(
        """INSERT OR REPLACE INTO artist_catalog (
            artist_name_normalized, spotify_artist_id, spotify_artist_name,
            tracks_json, fetched_at, expires_at
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            normalize_artist(artist_name),
            spotify_artist_id,
            spotify_artist_name,
            tracks_json,
            now.isoformat(),
            expires_at.isoformat(),
        ),
    )
    await conn.commit()
