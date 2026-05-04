from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

import aiosqlite

from .models import FileStatus, LocalFile, MatchCandidate

_INSERT_FILE = """
INSERT INTO local_file (
    path, mtime, size, format, duration_ms,
    artist, title, album, track_number, metadata_source,
    status, spotify_track_id, match_confidence, match_method,
    first_seen_at, last_scanned_at
) VALUES (
    :path, :mtime, :size, :format, :duration_ms,
    :artist, :title, :album, :track_number, :metadata_source,
    :status, :spotify_track_id, :match_confidence, :match_method,
    :first_seen_at, :last_scanned_at
)
"""

_SELECT_FILE_BY_PATH = """
SELECT id, path, mtime, size, format, duration_ms,
       artist, title, album, track_number, metadata_source,
       status, spotify_track_id, match_confidence, match_method,
       first_seen_at, last_scanned_at, last_error, last_run_id
FROM local_file WHERE path = ?
"""


def _row_to_local_file(row: tuple) -> LocalFile:
    return LocalFile(
        id=row[0], path=row[1], mtime=row[2], size=row[3], format=row[4],
        duration_ms=row[5], artist=row[6], title=row[7], album=row[8],
        track_number=row[9], metadata_source=row[10],
        status=FileStatus(row[11]), spotify_track_id=row[12],
        match_confidence=row[13], match_method=row[14],
        first_seen_at=row[15], last_scanned_at=row[16],
        last_error=row[17], last_run_id=row[18],
    )


async def get_local_file_by_path(conn: aiosqlite.Connection, path: str) -> LocalFile | None:
    cur = await conn.execute(_SELECT_FILE_BY_PATH, (path,))
    row = await cur.fetchone()
    return _row_to_local_file(row) if row else None


async def upsert_local_file(
    conn: aiosqlite.Connection,
    f: LocalFile,
    *,
    now: datetime,
) -> bool:
    """Insert or update a local file row.

    Returns True if a change was made (new file or content changed),
    False if (path, mtime, size) all matched existing row.
    """
    iso = now.isoformat()
    existing = await get_local_file_by_path(conn, f.path)
    if existing is None:
        await conn.execute(
            _INSERT_FILE,
            {
                "path": f.path, "mtime": f.mtime, "size": f.size, "format": f.format,
                "duration_ms": f.duration_ms,
                "artist": f.artist, "title": f.title, "album": f.album,
                "track_number": f.track_number, "metadata_source": f.metadata_source,
                "status": f.status.value, "spotify_track_id": f.spotify_track_id,
                "match_confidence": f.match_confidence, "match_method": f.match_method,
                "first_seen_at": iso, "last_scanned_at": iso,
            },
        )
        await conn.commit()
        return True
    if existing.mtime == f.mtime and existing.size == f.size:
        return False
    await conn.execute(
        """UPDATE local_file SET mtime=?, size=?, status='new', last_scanned_at=?
           WHERE path=?""",
        (f.mtime, f.size, iso, f.path),
    )
    await conn.commit()
    return True


async def touch_last_scanned(conn: aiosqlite.Connection, path: str, now: datetime) -> None:
    await conn.execute(
        "UPDATE local_file SET last_scanned_at=? WHERE path=?",
        (now.isoformat(), path),
    )
    await conn.commit()


async def mark_missing_files(conn: aiosqlite.Connection, *, scan_started: datetime) -> int:
    cur = await conn.execute(
        """UPDATE local_file SET status='missing'
           WHERE last_scanned_at < ? AND status != 'missing'""",
        (scan_started.isoformat(),),
    )
    await conn.commit()
    return cur.rowcount


async def count_by_status(conn: aiosqlite.Connection) -> dict[FileStatus, int]:
    cur = await conn.execute(
        "SELECT status, COUNT(*) FROM local_file GROUP BY status"
    )
    out: dict[FileStatus, int] = defaultdict(int)
    for status, n in await cur.fetchall():
        out[FileStatus(status)] = n
    return out


async def update_match(
    conn: aiosqlite.Connection,
    file_id: int,
    *,
    spotify_track_id: str,
    confidence: float,
    method: str,
    status: FileStatus = FileStatus.MATCHED,
) -> None:
    await conn.execute(
        """UPDATE local_file
           SET spotify_track_id=?, match_confidence=?, match_method=?, status=?
           WHERE id=?""",
        (spotify_track_id, confidence, method, status.value, file_id),
    )
    await conn.commit()


async def set_status(
    conn: aiosqlite.Connection, file_id: int, status: FileStatus,
    *, last_error: str | None = None,
) -> None:
    await conn.execute(
        "UPDATE local_file SET status=?, last_error=? WHERE id=?",
        (status.value, last_error, file_id),
    )
    await conn.commit()


async def clear_candidates(conn: aiosqlite.Connection, file_id: int) -> None:
    """Drop any prior match_candidate rows for this file.

    Called whenever a file's identity changes (re-tag via AI/AcoustID, fresh
    Spotify match) so the review queue can't display candidates that were
    fetched against a now-outdated artist/title.
    """
    await conn.execute(
        "DELETE FROM match_candidate WHERE local_file_id=?", (file_id,),
    )


async def insert_candidates(
    conn: aiosqlite.Connection,
    file_id: int,
    candidates: Iterable[MatchCandidate],
    *,
    now: datetime,
) -> None:
    iso = now.isoformat()
    rows = [
        (
            file_id, c.spotify_track_id, c.spotify_artist, c.spotify_title,
            c.spotify_album, c.spotify_duration_ms,
            c.artist_similarity, c.title_similarity, c.duration_delta_ms,
            c.confidence, c.rank, iso,
        )
        for c in candidates
    ]
    await conn.executemany(
        """INSERT INTO match_candidate (
            local_file_id, spotify_track_id, spotify_artist, spotify_title,
            spotify_album, spotify_duration_ms,
            artist_similarity, title_similarity, duration_delta_ms,
            confidence, rank, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    await conn.commit()


async def list_files_by_status(
    conn: aiosqlite.Connection, status: FileStatus, *, limit: int = 100, offset: int = 0,
) -> list[LocalFile]:
    cur = await conn.execute(
        _SELECT_FILE_BY_PATH.replace(
            "WHERE path = ?",
            "WHERE status = ? ORDER BY artist, album, track_number, title LIMIT ? OFFSET ?",
        ),
        (status.value, limit, offset),
    )
    return [_row_to_local_file(r) for r in await cur.fetchall()]


async def list_unique_artists(conn: aiosqlite.Connection) -> list[str]:
    cur = await conn.execute(
        "SELECT DISTINCT artist FROM local_file WHERE status='scanned' AND artist IS NOT NULL"
    )
    return [r[0] for r in await cur.fetchall()]
