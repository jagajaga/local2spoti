import httpx
import pytest
import respx

from local2spoti.ratelimit import TokenBucket
from local2spoti.spotify_client import SpotifyClient


@pytest.fixture
def client():
    bucket = TokenBucket(rate=1000, capacity=100)
    return SpotifyClient(access_token="fake", bucket=bucket)


@respx.mock
async def test_search_tracks(client):
    respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": [
            {"id": "abc", "name": "Around the World",
             "artists": [{"name": "Daft Punk"}],
             "album": {"name": "Homework"}, "duration_ms": 423000}
        ]}})
    )
    items = await client.search_tracks("Daft Punk", "Around the World", limit=5)
    assert len(items) == 1
    assert items[0]["id"] == "abc"


@respx.mock
async def test_search_artist(client):
    respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(200, json={"artists": {"items": [
            {"id": "xyz", "name": "Daft Punk"}
        ]}})
    )
    artist = await client.search_artist("Daft Punk")
    assert artist["id"] == "xyz"


@respx.mock
async def test_artist_albums(client):
    respx.get("https://api.spotify.com/v1/artists/xyz/albums").mock(
        return_value=httpx.Response(200, json={"items": [
            {"id": "alb1", "name": "Homework"},
            {"id": "alb2", "name": "Discovery"},
        ], "next": None})
    )
    albums = await client.artist_albums("xyz")
    assert [a["id"] for a in albums] == ["alb1", "alb2"]


@respx.mock
async def test_albums_batch(client):
    respx.get("https://api.spotify.com/v1/albums").mock(
        return_value=httpx.Response(200, json={"albums": [
            {"id": "alb1", "name": "Homework",
             "tracks": {"items": [
                 {"id": "t1", "name": "Da Funk", "duration_ms": 322000,
                  "artists": [{"name": "Daft Punk"}]}
             ]}},
        ]})
    )
    albums = await client.albums_batch(["alb1"])
    assert albums[0]["tracks"]["items"][0]["id"] == "t1"


@respx.mock
async def test_429_respects_retry_after(client):
    route = respx.get("https://api.spotify.com/v1/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"tracks": {"items": []}}),
        ]
    )
    items = await client.search_tracks("a", "b")
    assert items == []
    assert route.call_count == 2
