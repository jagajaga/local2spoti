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
        return_value=httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "abc",
                            "name": "Around the World",
                            "artists": [{"name": "Daft Punk"}],
                            "album": {"name": "Homework"},
                            "duration_ms": 423000,
                        }
                    ]
                }
            },
        )
    )
    items = await client.search_tracks("Daft Punk", "Around the World", limit=5)
    assert len(items) == 1
    assert items[0]["id"] == "abc"


@respx.mock
async def test_search_track_by_isrc_returns_first_track(client):
    """ISRC lookup is q=isrc:XXX&type=track&limit=1; we return the single
    match (or None for an empty items array)."""
    route = respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "deterministic-id",
                            "name": "Around the World",
                            "artists": [{"name": "Daft Punk"}],
                            "duration_ms": 423000,
                        },
                    ]
                }
            },
        )
    )
    track = await client.search_track_by_isrc("GBAYE9700675")
    assert track is not None
    assert track["id"] == "deterministic-id"
    # Verify we sent q=isrc:XXX so Spotify's catalog index is used.
    assert route.calls[0].request.url.params["q"] == "isrc:GBAYE9700675"
    assert route.calls[0].request.url.params["type"] == "track"


@respx.mock
async def test_search_track_by_isrc_returns_none_on_miss(client):
    """Empty items → no match → None. Caller falls through to fuzzy match."""
    respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": []}})
    )
    assert await client.search_track_by_isrc("XX1234567890") is None


@respx.mock
async def test_search_artist(client):
    respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(200, json={"artists": {"items": [{"id": "xyz", "name": "Daft Punk"}]}})
    )
    artist = await client.search_artist("Daft Punk")
    assert artist["id"] == "xyz"


@respx.mock
async def test_search_artist_rejects_low_similarity_top_result(client):
    """Regression: Spotify's /search?type=artist sometimes returns a more
    popular adjacent artist as the top result (Mozart → Beethoven,
    DJ Shadow → Massive Attack). Refuse when no candidate matches the
    query name to avoid caching a wrong 47K-track catalog for 30 days.
    """
    respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "artists": {
                    "items": [
                        # All five hits are wrong popular-classical names
                        # for a "Mozart" query — should reject.
                        {"id": "1", "name": "Ludwig van Beethoven"},
                        {"id": "2", "name": "Johann Sebastian Bach"},
                        {"id": "3", "name": "Berliner Philharmoniker"},
                        {"id": "4", "name": "Pyotr Ilyich Tchaikovsky"},
                        {"id": "5", "name": "Antonio Vivaldi"},
                    ]
                }
            },
        )
    )
    assert await client.search_artist("Mozart") is None


@respx.mock
async def test_search_artist_picks_best_among_top5(client):
    """Top result isn't always the right one — pick whichever of the
    top-5 actually matches the query name."""
    respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "artists": {
                    "items": [
                        {"id": "wrong1", "name": "Some Compilation Artist"},
                        {"id": "wrong2", "name": "Various Artists"},
                        {"id": "right", "name": "DJ Shadow"},  # the actual hit at rank 3
                        {"id": "wrong3", "name": "Shadow Gallery"},
                        {"id": "wrong4", "name": "DJ Shadowboxxer"},
                    ]
                }
            },
        )
    )
    artist = await client.search_artist("DJ Shadow")
    assert artist is not None
    assert artist["id"] == "right"


@respx.mock
async def test_search_artist_rejects_substring_containment(client):
    """Regression: short artist names are catastrophic with substring-
    aware fuzzy ratios. 'Family' matched 'Family of the Year' with 0.9
    WRatio, polluting the catalog cache. The Levenshtein-ratio path
    must score that pair below the 0.85 cutoff and reject.
    """
    respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "artists": {
                    "items": [
                        {"id": "wrong", "name": "Family of the Year"},
                        {"id": "other", "name": "Family Force 5"},
                        {"id": "other2", "name": "The Family Crest"},
                    ]
                }
            },
        )
    )
    assert await client.search_artist("Family") is None


@respx.mock
async def test_search_artist_exact_match_wins_regardless_of_rank(client):
    """When an exact (case/punctuation-insensitive) name match is in
    the top 5, pick it even if Spotify ranked something else higher.
    """
    respx.get("https://api.spotify.com/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "artists": {
                    "items": [
                        {"id": "big", "name": "Family of the Year"},
                        {"id": "exact", "name": "FAMILY"},  # rank 2 but exact (ci)
                        {"id": "other", "name": "Family Force 5"},
                    ]
                }
            },
        )
    )
    artist = await client.search_artist("Family")
    assert artist is not None
    assert artist["id"] == "exact"


@respx.mock
async def test_artist_albums(client):
    respx.get("https://api.spotify.com/v1/artists/xyz/albums").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"id": "alb1", "name": "Homework"},
                    {"id": "alb2", "name": "Discovery"},
                ],
                "next": None,
            },
        )
    )
    albums = await client.artist_albums("xyz")
    assert [a["id"] for a in albums] == ["alb1", "alb2"]


@respx.mock
async def test_albums_batch(client):
    respx.get("https://api.spotify.com/v1/albums").mock(
        return_value=httpx.Response(
            200,
            json={
                "albums": [
                    {
                        "id": "alb1",
                        "name": "Homework",
                        "tracks": {
                            "items": [
                                {
                                    "id": "t1",
                                    "name": "Da Funk",
                                    "duration_ms": 322000,
                                    "artists": [{"name": "Daft Punk"}],
                                }
                            ]
                        },
                    },
                ]
            },
        )
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
                json={"error": {"status": 403, "message": "Spotify is unavailable in this country"}},
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
            json={"error": {"status": 403, "message": "Insufficient client scope"}},
        )
    )
    with pytest.raises(SpotifyError):
        await client.search_tracks("a", "b")


def test_soft_rate_limit_403_helper_recognizes_known_messages():
    import httpx as _httpx

    from local2spoti.spotify_client import _is_soft_rate_limit_403

    def _mk(status, body):
        # Build a stripped-down Response with the right status and body
        return _httpx.Response(
            status,
            content=body.encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    assert _is_soft_rate_limit_403(
        _mk(403, '{"error":{"status":403,"message":"Spotify is unavailable in this country"}}')
    )
    assert _is_soft_rate_limit_403(_mk(403, '{"error":"rate limit exceeded"}'))
    assert not _is_soft_rate_limit_403(_mk(403, '{"error":{"message":"insufficient scope"}}'))
    assert not _is_soft_rate_limit_403(_mk(401, '{"error":"unauthorized"}'))
    assert not _is_soft_rate_limit_403(_mk(200, "{}"))


@respx.mock
async def test_connection_error_retried_until_success(client, monkeypatch):
    """Network blips (ConnectError, TimeoutException, etc.) are NOT a
    file-level failure — pause and retry, like a 429.

    Patch the default pause to a tiny value so the test stays fast; we're
    proving the retry behavior, not waiting out the real 60s default.
    """
    monkeypatch.setattr(
        "local2spoti.spotify_client._DEFAULT_RATE_LIMIT_PAUSE_SECONDS",
        0.01,
    )
    route = respx.get("https://api.spotify.com/v1/search").mock(
        side_effect=[
            httpx.ConnectError("All connection attempts failed"),
            httpx.ReadTimeout("read timed out"),
            httpx.Response(200, json={"tracks": {"items": []}}),
        ]
    )
    items = await client.search_tracks("a", "b")
    assert items == []
    assert route.call_count == 3  # both transient errors retried, third succeeded
