from __future__ import annotations

from dataclasses import dataclass

from .normalize import alpha_bucket


@dataclass(slots=True)
class PlaylistChunkPlan:
    chunk_index: int
    alpha_range: str
    name: str
    track_ids: list[str]


def chunk_files_alpha(
    files: list[dict],
    *,
    chunk_size: int = 9000,
) -> list[PlaylistChunkPlan]:
    sorted_files = sorted(files, key=lambda f: (f.get("artist") or "").lower())
    chunks: list[PlaylistChunkPlan] = []
    buffer: list[dict] = []
    for f in sorted_files:
        buffer.append(f)
        if len(buffer) >= chunk_size:
            chunks.append(_buffer_to_chunk(buffer, chunk_index=len(chunks) + 1))
            buffer = []
    if buffer:
        chunks.append(_buffer_to_chunk(buffer, chunk_index=len(chunks) + 1))

    total = len(chunks)
    for c in chunks:
        c.name = f"Local Library {c.chunk_index}/{total} ({c.alpha_range})"
    return chunks


def _buffer_to_chunk(buffer: list[dict], *, chunk_index: int) -> PlaylistChunkPlan:
    first = alpha_bucket(buffer[0]["artist"] or "")
    last = alpha_bucket(buffer[-1]["artist"] or "")
    alpha_range = first if first == last else f"{first}-{last}"
    return PlaylistChunkPlan(
        chunk_index=chunk_index,
        alpha_range=alpha_range,
        name=f"Local Library {chunk_index} ({alpha_range})",
        track_ids=[f["spotify_track_id"] for f in buffer],
    )


from datetime import UTC, datetime

import aiosqlite

from .spotify_client import SpotifyClient


@dataclass(slots=True)
class PushResult:
    playlists_created: int
    added: int


async def push_matched_to_spotify(
    *, conn: aiosqlite.Connection, client: SpotifyClient,
) -> PushResult:
    """For all matched files not yet in any playlist, create chunked playlists and add."""
    cur = await conn.execute(
        """SELECT lf.id, lf.artist, lf.album, lf.track_number, lf.title, lf.spotify_track_id
           FROM local_file lf
           LEFT JOIN playlist_track pt ON pt.local_file_id = lf.id
           WHERE lf.status='matched' AND pt.local_file_id IS NULL
           ORDER BY lf.artist, lf.album, lf.track_number, lf.title"""
    )
    rows = await cur.fetchall()
    if not rows:
        return PushResult(playlists_created=0, added=0)

    files_dicts = [
        {"file_id": r[0], "artist": r[1], "spotify_track_id": r[5]}
        for r in rows
    ]
    chunks = chunk_files_alpha(files_dicts, chunk_size=9000)

    me = await client.me()
    user_id = me["id"]
    now_iso = datetime.now(UTC).isoformat()
    created = 0
    added = 0

    for chunk in chunks:
        sp = await client.create_playlist(user_id, chunk.name, public=False)
        spotify_playlist_id = sp["id"]
        cur = await conn.execute(
            """INSERT INTO playlist (spotify_playlist_id, name, chunk_index, alpha_range,
                                     created_at, track_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (spotify_playlist_id, chunk.name, chunk.chunk_index, chunk.alpha_range,
             now_iso, len(chunk.track_ids)),
        )
        playlist_db_id = cur.lastrowid
        await conn.commit()

        uris = [f"spotify:track:{tid}" for tid in chunk.track_ids]
        await client.add_tracks(spotify_playlist_id, uris)

        chunk_files = [f for f in files_dicts if f["spotify_track_id"] in set(chunk.track_ids)]
        rows_to_insert = [
            (playlist_db_id, f["file_id"], f["spotify_track_id"], now_iso)
            for f in chunk_files
        ]
        await conn.executemany(
            """INSERT INTO playlist_track (playlist_id, local_file_id, spotify_track_id, added_at)
               VALUES (?, ?, ?, ?)""",
            rows_to_insert,
        )
        await conn.commit()
        created += 1
        added += len(chunk.track_ids)

    return PushResult(playlists_created=created, added=added)
