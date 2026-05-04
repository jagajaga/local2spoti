from local2spoti.playlist import chunk_files_alpha


def _files(*artists):
    return [{"artist": a, "spotify_track_id": f"t{i}"} for i, a in enumerate(artists)]


def test_single_chunk_under_capacity():
    files = _files(*[f"Artist{i}" for i in range(50)])
    chunks = chunk_files_alpha(files, chunk_size=9000)
    assert len(chunks) == 1
    assert chunks[0].alpha_range.startswith("A")
    assert len(chunks[0].track_ids) == 50


def test_alpha_split_when_over_capacity():
    artists = (
        [f"A{i:04d}" for i in range(5000)]
        + [f"M{i:04d}" for i in range(5000)]
        + [f"Z{i:04d}" for i in range(2000)]
    )
    files = _files(*artists)
    chunks = chunk_files_alpha(files, chunk_size=9000)
    assert len(chunks) >= 2
    assert sum(len(c.track_ids) for c in chunks) == 12000
    for c in chunks:
        assert c.alpha_range


def test_chunk_index_starts_at_one():
    chunks = chunk_files_alpha(_files("A", "B"), chunk_size=9000)
    assert chunks[0].chunk_index == 1


def test_chunk_name_contains_index_and_total():
    files = _files(*[f"A{i:04d}" for i in range(10000)] + [f"Z{i:04d}" for i in range(2000)])
    chunks = chunk_files_alpha(files, chunk_size=9000)
    names = [c.name for c in chunks]
    for i, n in enumerate(names, start=1):
        assert f"{i}/{len(chunks)}" in n


from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from local2spoti.db import connect, init_schema
from local2spoti.playlist import push_matched_to_spotify
from local2spoti.models import FileStatus, LocalFile
from local2spoti import repo


async def test_push_creates_playlists_and_inserts_track_rows(tmp_path):
    db = tmp_path / "t.db"
    client = AsyncMock()
    client.me.return_value = {"id": "user1"}
    client.create_playlist.return_value = {"id": "spotPlay1", "name": "Local Library 1/1"}
    client.add_tracks.return_value = None

    async with connect(db) as conn:
        await init_schema(conn)
        now = datetime(2026, 5, 4, tzinfo=UTC)
        for i in range(3):
            await repo.upsert_local_file(conn, LocalFile(
                path=f"/{i}.mp3", mtime=1, size=1, format="mp3",
                artist="Daft Punk", title=f"T{i}",
                spotify_track_id=f"track{i}",
                status=FileStatus.MATCHED,
            ), now=now)
        result = await push_matched_to_spotify(conn=conn, client=client)
        assert result.added == 3
        assert client.add_tracks.await_count == 1
        cur = await conn.execute("SELECT COUNT(*) FROM playlist_track")
        assert (await cur.fetchone())[0] == 3
