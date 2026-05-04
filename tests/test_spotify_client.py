import httpx
import pytest
import respx

from local2spoti.ratelimit import TokenBucket
from local2spoti.spotify_client import SpotifyClient, SpotifyError


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


@respx.mock
async def test_403_unavailable_in_country_treated_as_rate_limit(client):
    """Spotify escalates 429→403-with-geo-message after sustained throttling.
    The client should treat that as a soft rate limit (pause + retry the
    same request) rather than raising — the file is fine, only Spotify
    is being grumpy."""
    route = respx.get("https://api.spotify.com/v1/search").mock(
        side_effect=[
            httpx.Response(
                403,
                headers={"Retry-After": "0"},
                json={"error": {"status": 403,
                                "message": "Spotify is unavailable in this country"}},
            ),
            httpx.Response(200, json={"tracks": {"items": []}}),
        ]
    )
    items = await client.search_tracks("a", "b")
    assert items == []
    assert route.call_count == 2  # paused, then retried


@respx.mock
async def test_real_403_still_raises(client):
    """A 403 with a non-rate-limit body (e.g. invalid scope) should still
    raise — only the soft-rate-limit pattern gets retried."""
    respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(
            403,
            json={"error": {"status": 403,
                            "message": "Insufficient client scope"}},
        )
    )
    with pytest.raises(SpotifyError):
        await client.search_tracks("a", "b")


def test_soft_rate_limit_403_helper_recognizes_known_messages():
    from local2spoti.spotify_client import _is_soft_rate_limit_403
    import httpx as _httpx

    def _mk(status, body):
        # Build a stripped-down Response with the right status and body
        return _httpx.Response(
            status, content=body.encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    assert _is_soft_rate_limit_403(_mk(403,
        '{"error":{"status":403,"message":"Spotify is unavailable in this country"}}'))
    assert _is_soft_rate_limit_403(_mk(403, '{"error":"rate limit exceeded"}'))
    assert not _is_soft_rate_limit_403(_mk(403, '{"error":{"message":"insufficient scope"}}'))
    assert not _is_soft_rate_limit_403(_mk(401, '{"error":"unauthorized"}'))
    assert not _is_soft_rate_limit_403(_mk(200, '{}'))
