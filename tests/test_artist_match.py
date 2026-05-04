from unittest.mock import AsyncMock

import pytest

from local2spoti.artist_match import match_artist_group
from local2spoti.matcher import Threshold
from local2spoti.models import LocalFile, FileStatus


def _file(artist: str, title: str, dur: int = 423000) -> LocalFile:
    return LocalFile(
        path=f"/{title}.mp3", mtime=1, size=1, format="mp3",
        artist=artist, title=title, duration_ms=dur, status=FileStatus.SCANNED,
    )


@pytest.fixture
def fake_client():
    client = AsyncMock()
    client.search_artist.return_value = {"id": "artist1", "name": "Daft Punk"}
    client.artist_albums.return_value = [
        {"id": "alb1", "name": "Homework"},
    ]
    client.albums_batch.return_value = [
        {"id": "alb1", "name": "Homework", "tracks": {"items": [
            {"id": "t1", "name": "Around the World", "duration_ms": 423000,
             "artists": [{"name": "Daft Punk"}]},
            {"id": "t2", "name": "Da Funk", "duration_ms": 322000,
             "artists": [{"name": "Daft Punk"}]},
        ]}},
    ]
    return client


async def test_artist_match_finds_correct_track(fake_client):
    files = [_file("Daft Punk", "Around the World", 423000)]
    results = await match_artist_group(
        client=fake_client, artist="Daft Punk", files=files, threshold=Threshold.BALANCED,
    )
    [r] = results
    assert r.decision == "auto"
    assert r.top_candidate.spotify_track_id == "t1"


async def test_artist_match_review_for_typo(fake_client):
    files = [_file("Daft Punk", "Arond Da World", 423000)]
    results = await match_artist_group(
        client=fake_client, artist="Daft Punk", files=files, threshold=Threshold.BALANCED,
    )
    [r] = results
    assert r.decision in ("review", "auto")
    assert r.top_candidate is not None


async def test_artist_match_unmatched_when_no_match(fake_client):
    files = [_file("Daft Punk", "Some Track Not In Catalog", 200000)]
    results = await match_artist_group(
        client=fake_client, artist="Daft Punk", files=files, threshold=Threshold.STRICT,
    )
    [r] = results
    assert r.decision in ("review", "unmatched")


async def test_artist_match_no_artist_results():
    client = AsyncMock()
    client.search_artist.return_value = None
    files = [_file("Mystery Artist", "Track")]
    results = await match_artist_group(
        client=client, artist="Mystery Artist", files=files, threshold=Threshold.BALANCED,
    )
    [r] = results
    assert r.decision == "no_artist"


from local2spoti.artist_match import match_per_track


async def test_per_track_fallback_finds_match():
    client = AsyncMock()
    client.search_tracks.return_value = [
        {"id": "t1", "name": "Around the World", "duration_ms": 423000,
         "artists": [{"name": "Daft Punk"}],
         "album": {"name": "Homework"}},
    ]
    f = LocalFile(path="/x.mp3", mtime=1, size=1, format="mp3",
                  artist="Daft Punk", title="Around the World",
                  duration_ms=423000, status=FileStatus.SCANNED)
    [r] = await match_per_track(client=client, files=[f], threshold=Threshold.BALANCED)
    assert r.decision == "auto"
    assert r.top_candidate.spotify_track_id == "t1"


async def test_per_track_fallback_no_results():
    client = AsyncMock()
    client.search_tracks.return_value = []
    f = LocalFile(path="/x.mp3", mtime=1, size=1, format="mp3",
                  artist="Mystery", title="Mystery", status=FileStatus.SCANNED)
    [r] = await match_per_track(client=client, files=[f], threshold=Threshold.BALANCED)
    assert r.decision == "unmatched"
