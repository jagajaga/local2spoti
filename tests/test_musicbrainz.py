"""MusicBrainz Recording → Spotify track ID resolver.

Mocked at the HTTP layer; we don't actually hit musicbrainz.org.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from local2spoti.musicbrainz import MusicBrainzClient


def _mb_recording_response(relations: list[dict]) -> dict:
    return {
        "id": "abcd-1234",
        "title": "Test Track",
        "relations": relations,
    }


@respx.mock
async def test_resolves_spotify_url_from_free_streaming_relationship():
    respx.get("https://musicbrainz.org/ws/2/recording/abcd-1234").mock(
        return_value=httpx.Response(200, json=_mb_recording_response([
            {
                "type": "free streaming",
                "url": {"resource": "https://open.spotify.com/track/4iV5W9uYEdYUVa79Axb7Rh"},
            }
        ]))
    )
    client = MusicBrainzClient()
    try:
        track_id = await client.spotify_track_id_for_mbid("abcd-1234")
    finally:
        await client.aclose()
    assert track_id == "4iV5W9uYEdYUVa79Axb7Rh"


@respx.mock
async def test_resolves_spotify_uri_format():
    """`spotify:track:<id>` should also be recognized, in case MB stores
    the URI form rather than the open.spotify.com URL."""
    respx.get("https://musicbrainz.org/ws/2/recording/abcd-1234").mock(
        return_value=httpx.Response(200, json=_mb_recording_response([
            {"type": "streaming", "url": {"resource": "spotify:track:4iV5W9uYEdYUVa79Axb7Rh"}}
        ]))
    )
    client = MusicBrainzClient()
    try:
        track_id = await client.spotify_track_id_for_mbid("abcd-1234")
    finally:
        await client.aclose()
    assert track_id == "4iV5W9uYEdYUVa79Axb7Rh"


@respx.mock
async def test_returns_none_when_no_spotify_relationship():
    respx.get("https://musicbrainz.org/ws/2/recording/abcd-1234").mock(
        return_value=httpx.Response(200, json=_mb_recording_response([
            {"type": "free streaming",
             "url": {"resource": "https://music.youtube.com/watch?v=xyz"}},
            {"type": "free streaming",
             "url": {"resource": "https://www.deezer.com/track/12345"}},
        ]))
    )
    client = MusicBrainzClient()
    try:
        track_id = await client.spotify_track_id_for_mbid("abcd-1234")
    finally:
        await client.aclose()
    assert track_id is None


@respx.mock
async def test_returns_none_on_404():
    respx.get("https://musicbrainz.org/ws/2/recording/missing").mock(
        return_value=httpx.Response(404, json={"error": "Not Found"})
    )
    client = MusicBrainzClient()
    try:
        track_id = await client.spotify_track_id_for_mbid("missing")
    finally:
        await client.aclose()
    assert track_id is None


@respx.mock
async def test_returns_none_on_network_error():
    respx.get("https://musicbrainz.org/ws/2/recording/abcd").mock(
        side_effect=httpx.ConnectError("dns"),
    )
    client = MusicBrainzClient()
    try:
        track_id = await client.spotify_track_id_for_mbid("abcd")
    finally:
        await client.aclose()
    assert track_id is None


@respx.mock
async def test_follows_301_redirect_when_mbid_was_merged():
    """MB returns 301 with a Location header when the requested MBID has
    been merged into another recording. We want to follow that redirect
    and return whatever the canonical recording's Spotify URL is.
    """
    # Simulate: old MBID redirects to canonical, canonical has a Spotify URL.
    old_mbid = "0e421515-3407-4615-b780-1fb31499bd68"
    new_mbid = "11111111-2222-3333-4444-555555555555"
    respx.get(f"https://musicbrainz.org/ws/2/recording/{old_mbid}").mock(
        return_value=httpx.Response(
            301,
            headers={
                "Location": (
                    f"https://musicbrainz.org/ws/2/recording/{new_mbid}"
                    "?inc=url-rels&fmt=json"
                ),
            },
        )
    )
    respx.get(f"https://musicbrainz.org/ws/2/recording/{new_mbid}").mock(
        return_value=httpx.Response(200, json={
            "id": new_mbid,
            "title": "Some Track",
            "relations": [
                {
                    "type": "free streaming",
                    "url": {"resource": "https://open.spotify.com/track/AAAAAAAAAAAAAAAAAAAAAA"},
                },
            ],
        })
    )
    client = MusicBrainzClient()
    try:
        result = await client.spotify_track_id_for_mbid(old_mbid)
    finally:
        await client.aclose()
    assert result == "AAAAAAAAAAAAAAAAAAAAAA"
