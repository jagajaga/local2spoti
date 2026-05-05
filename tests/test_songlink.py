"""SongLink/Odesli URL → Spotify track ID resolver tests.

Mocked at the HTTP layer; we don't actually hit api.song.link.
"""

from __future__ import annotations

import httpx
import respx

from local2spoti.songlink import SongLinkClient


@respx.mock
async def test_resolves_apple_music_url_to_spotify_track_id():
    apple_url = "https://music.apple.com/us/album/abc/123?i=456"
    respx.get("https://api.song.link/v1-alpha.1/links").mock(
        return_value=httpx.Response(200, json={
            "linksByPlatform": {
                "spotify": {
                    "url": "https://open.spotify.com/track/4iV5W9uYEdYUVa79Axb7Rh",
                },
                "appleMusic": {"url": apple_url},
            },
        })
    )
    client = SongLinkClient()
    try:
        track_id = await client.spotify_track_id_from_url(apple_url)
    finally:
        await client.aclose()
    assert track_id == "4iV5W9uYEdYUVa79Axb7Rh"


@respx.mock
async def test_returns_none_when_no_spotify_in_response():
    """Some niche releases have an Apple URL on Odesli but no Spotify
    equivalent. We treat that as a clean miss."""
    respx.get("https://api.song.link/v1-alpha.1/links").mock(
        return_value=httpx.Response(200, json={
            "linksByPlatform": {
                "appleMusic": {"url": "https://music.apple.com/us/album/x/1"},
                "deezer":     {"url": "https://www.deezer.com/track/2"},
            },
        })
    )
    client = SongLinkClient()
    try:
        track_id = await client.spotify_track_id_from_url(
            "https://music.apple.com/us/album/x/1",
        )
    finally:
        await client.aclose()
    assert track_id is None


@respx.mock
async def test_returns_none_on_429():
    """Rate limit hit → soft miss; caller falls through to next strategy."""
    respx.get("https://api.song.link/v1-alpha.1/links").mock(
        return_value=httpx.Response(429, json={"error": "rate limited"})
    )
    client = SongLinkClient()
    try:
        track_id = await client.spotify_track_id_from_url(
            "https://music.apple.com/us/album/x/1",
        )
    finally:
        await client.aclose()
    assert track_id is None


@respx.mock
async def test_returns_none_on_network_error():
    respx.get("https://api.song.link/v1-alpha.1/links").mock(
        side_effect=httpx.ConnectError("dns"),
    )
    client = SongLinkClient()
    try:
        track_id = await client.spotify_track_id_from_url(
            "https://music.apple.com/us/album/x/1",
        )
    finally:
        await client.aclose()
    assert track_id is None


@respx.mock
async def test_returns_none_on_invalid_json():
    respx.get("https://api.song.link/v1-alpha.1/links").mock(
        return_value=httpx.Response(200, text="not json"),
    )
    client = SongLinkClient()
    try:
        track_id = await client.spotify_track_id_from_url(
            "https://music.apple.com/us/album/x/1",
        )
    finally:
        await client.aclose()
    assert track_id is None
