"""SongLink/Odesli cross-platform URL → Spotify track ID resolver.

When MusicBrainz has *some* streaming URL on a recording (Apple Music,
Deezer, Tidal, YouTube Music, SoundCloud) but not a Spotify URL, Odesli
can do the cross-platform lookup for us — free, no auth, just a GET.
That saves a `/v1/search` round trip on Spotify (which is where the
rate limits bite hardest) for every track MB has registered on at least
one streaming service.

API: https://api.song.link/v1-alpha.1/links?url=<encoded_url>
Public limit: 10 requests/min. We rate-limit ourselves at slightly under
that and treat 429s as a soft miss (return None) so a temporary throttle
just falls through to the regular match path.
"""

from __future__ import annotations

import re

import httpx

from . import __version__
from .ratelimit import TokenBucket

_ODESLI_BASE = "https://api.song.link/v1-alpha.1"

_USER_AGENT = f"Local2Spoti/{__version__} ( https://github.com/local2spoti - local audio library to Spotify )"

# Odesli's documented public limit is 10 rpm. Pick 0.15/sec (= 9/min) to
# stay safely under it; capacity=2 lets quick bursts through without
# trickle-stalling the deep_scan loop.
_ODESLI_BUCKET = TokenBucket(rate=0.15, capacity=2.0)

_SPOTIFY_TRACK_URL_RE = re.compile(r"(?:https?://open\.spotify\.com/track/|spotify:track:)([A-Za-z0-9]{22})")


class SongLinkClient:
    """Thin async client for the Odesli/SongLink public API."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=_ODESLI_BASE,
            timeout=15.0,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def spotify_track_id_from_url(self, url: str) -> str | None:
        """Resolve any streaming URL to a Spotify track ID, if Odesli has
        the cross-platform mapping. Returns None on miss / failure /
        non-Spotify result — caller treats every None as a soft miss
        and falls back to whatever's next in the chain.
        """
        await _ODESLI_BUCKET.acquire()
        try:
            r = await self._http.get("/links", params={"url": url})
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError):
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        # Odesli returns a `linksByPlatform` dict keyed by platform name
        # ('spotify', 'appleMusic', 'deezer', ...). Each entry has a `url`
        # we can parse for the 22-char track ID.
        spotify = (data.get("linksByPlatform") or {}).get("spotify") or {}
        spotify_url = spotify.get("url") or ""
        m = _SPOTIFY_TRACK_URL_RE.search(spotify_url)
        if m:
            return m.group(1)
        return None
