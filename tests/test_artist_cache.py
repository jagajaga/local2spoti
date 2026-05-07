"""Tests for the SQLite-backed Spotify artist catalog cache."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from local2spoti import artist_cache
from local2spoti.artist_match import match_artist_group
from local2spoti.db import connect, init_schema
from local2spoti.matcher import Threshold
from local2spoti.models import FileStatus, LocalFile


@pytest.fixture
async def conn(tmp_path):
    async with connect(tmp_path / "t.db") as c:
        await init_schema(c)
        yield c


async def test_cache_miss_returns_none(conn):
    assert await artist_cache.get(conn, "Beach Boys") is None


async def test_positive_round_trip(conn):
    tracks = [
        {
            "id": "t1",
            "name": "Track One",
            "duration_ms": 200000,
            "artists": [{"name": "Beach Boys"}],
            "album": {"name": "Pet Sounds"},
        }
    ]
    await artist_cache.put(
        conn,
        "Beach Boys",
        spotify_artist_id="art1",
        spotify_artist_name="The Beach Boys",
        tracks=tracks,
    )
    cached = await artist_cache.get(conn, "Beach Boys")
    assert cached is not None
    assert cached.is_positive
    assert cached.spotify_artist_id == "art1"
    assert cached.tracks == tracks


async def test_normalization_collapses_case(conn):
    """Normalized name is the cache key, so 'beach boys' and 'BEACH BOYS'
    hit the same row."""
    await artist_cache.put(
        conn,
        "Beach Boys",
        spotify_artist_id="art1",
        spotify_artist_name="The Beach Boys",
        tracks=[],
    )
    assert await artist_cache.get(conn, "BEACH BOYS") is not None
    assert await artist_cache.get(conn, "beach boys") is not None


async def test_negative_cache(conn):
    """Negative result also gets cached (so we don't re-search every time)."""
    await artist_cache.put(
        conn,
        "Definitely Not Real Artist",
        spotify_artist_id=None,
        spotify_artist_name=None,
        tracks=[],
    )
    cached = await artist_cache.get(conn, "Definitely Not Real Artist")
    assert cached is not None
    assert not cached.is_positive
    assert cached.tracks == []


async def test_expired_entry_treated_as_miss(conn):
    """When expires_at < now, get() returns None (caller will refresh)."""
    yesterday = datetime.now(UTC) - timedelta(days=2)
    # Inject an expired row directly so we don't have to wait
    await conn.execute(
        """INSERT INTO artist_catalog
           (artist_name_normalized, spotify_artist_id, spotify_artist_name,
            tracks_json, fetched_at, expires_at)
           VALUES ('expired', 'art1', 'Old Artist', '[]', ?, ?)""",
        (yesterday.isoformat(), (yesterday + timedelta(hours=1)).isoformat()),
    )
    await conn.commit()
    assert await artist_cache.get(conn, "expired") is None


@pytest.mark.asyncio
async def test_match_artist_group_uses_cache_on_second_call(conn):
    """The big payoff: on the second call for the same artist, no Spotify
    HTTP calls are made — match runs entirely off the cached catalog."""
    client = AsyncMock()
    client.search_artist.return_value = {"id": "art1", "name": "Daft Punk"}
    client.artist_albums.return_value = [{"id": "alb1", "name": "Homework"}]
    client.albums_batch.return_value = [
        {
            "id": "alb1",
            "name": "Homework",
            "tracks": {
                "items": [
                    {
                        "id": "t1",
                        "name": "Around the World",
                        "duration_ms": 423000,
                        "artists": [{"name": "Daft Punk"}],
                    },
                ]
            },
        }
    ]
    file = LocalFile(
        path="/x.mp3",
        mtime=1,
        size=1,
        format="mp3",
        artist="Daft Punk",
        title="Around the World",
        duration_ms=423000,
        status=FileStatus.SCANNED,
    )

    # First call → fetches from Spotify, stores in cache
    results = await match_artist_group(
        client=client,
        artist="Daft Punk",
        files=[file],
        threshold=Threshold.BALANCED,
        conn=conn,
    )
    assert results[0].decision == "auto"
    assert client.search_artist.await_count == 1
    assert client.artist_albums.await_count == 1
    assert client.albums_batch.await_count == 1

    # Second call → cache hit, no Spotify calls at all
    client.search_artist.reset_mock()
    client.artist_albums.reset_mock()
    client.albums_batch.reset_mock()
    results2 = await match_artist_group(
        client=client,
        artist="Daft Punk",
        files=[file],
        threshold=Threshold.BALANCED,
        conn=conn,
    )
    assert results2[0].decision == "auto"
    assert client.search_artist.await_count == 0
    assert client.artist_albums.await_count == 0
    assert client.albums_batch.await_count == 0


@pytest.mark.asyncio
async def test_negative_cache_short_circuits_no_artist(conn):
    """If a previous call cached 'no Spotify match for this name',
    subsequent files for that artist return no_artist immediately
    without hitting search."""
    await artist_cache.put(
        conn,
        "Mystery Artist",
        spotify_artist_id=None,
        spotify_artist_name=None,
        tracks=[],
    )
    client = AsyncMock()
    file = LocalFile(
        path="/x.mp3",
        mtime=1,
        size=1,
        format="mp3",
        artist="Mystery Artist",
        title="X",
        status=FileStatus.SCANNED,
    )
    results = await match_artist_group(
        client=client,
        artist="Mystery Artist",
        files=[file],
        threshold=Threshold.BALANCED,
        conn=conn,
    )
    assert results[0].decision == "no_artist"
    assert client.search_artist.await_count == 0  # cache short-circuit
