"""MusicBrainz Recording → Spotify track URL resolver.

The point: AcoustID identifies a fingerprint as a MusicBrainz Recording
(an MBID); MusicBrainz keeps URL relationships on each recording, and one
of the standard relationship types is 'free streaming' — which, for
mainstream tracks, points directly at `https://open.spotify.com/track/<id>`.

By chaining AcoustID → MBID → MusicBrainz URL rels we get Spotify track
IDs without ever calling `/v1/search`, which is where Spotify's rate
limits bite hardest. The MB API is free and asks for at most 1 req/sec
plus a User-Agent header — which we honor below.

Coverage is partial: editors fill in Spotify URLs on most commercial
releases but not on obscure / DJ-pool / podcast-derived content. So this
is strictly additive — when it resolves, we save a `/search` round trip;
when it doesn't, we fall back to the existing match path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from . import __version__
from .ratelimit import TokenBucket

_MB_BASE = "https://musicbrainz.org/ws/2"

# MusicBrainz requires a User-Agent identifying the application.
# Free; just be polite.
_USER_AGENT = (
    f"Local2Spoti/{__version__} "
    "( https://github.com/local2spoti - local audio library to Spotify )"
)

# 1 req/sec is the documented MB anonymous rate limit. capacity=2 lets
# us absorb a tiny burst (e.g. a quick double-call on the same recording)
# without trickle-stalling on perfectly steady-state requests.
_MB_BUCKET = TokenBucket(rate=1.0, capacity=2.0)

# Matches both `https://open.spotify.com/track/<id>` and
# `spotify:track:<id>`. Spotify track IDs are 22-char base62.
_SPOTIFY_TRACK_URL_RE = re.compile(
    r"(?:https?://open\.spotify\.com/track/|spotify:track:)([A-Za-z0-9]{22})"
)

# Hosts that Odesli/SongLink can resolve to a Spotify URL. Order matters
# only as a stable preference: Apple/iTunes is by far the most populated
# on MB recordings, so we try it first when more than one is available.
_ODESLI_RESOLVABLE_HOSTS = (
    "music.apple.com", "itunes.apple.com",
    "deezer.com", "www.deezer.com",
    "tidal.com", "www.tidal.com", "listen.tidal.com",
    "music.youtube.com",
    "soundcloud.com",
)

# Relationship `type` strings that historically carry Spotify track URLs.
# The relationship-type IDs are stable but the human-readable `type`
# strings are what you see in the JSON; we match either and fall back to
# scanning every URL on the recording for a Spotify track pattern.
_STREAMING_REL_TYPES = frozenset({
    "free streaming",
    "streaming",
})


class MusicBrainzClient:
    """Thin async client for the MusicBrainz JSON API."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=_MB_BASE,
            timeout=30.0,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            # Follow 301/302. MB redirects when an MBID has been merged
            # into another recording — the Location header points at the
            # canonical MBID. Without this we'd return None for every
            # merged recording, even though the destination has the
            # Spotify URL relationship we're after.
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def resolve_mbid(self, mbid: str) -> "MBResolution":
        """Fetch the MB recording and extract anything useful for matching.

        Returns:
          - `spotify_track_id`: the 22-char Spotify ID, when MB has a
            Spotify URL relationship on this recording (the happy path).
          - `odesli_url`: a non-Spotify streaming URL (Apple/Deezer/Tidal/
            YouTube Music/SoundCloud) that Odesli can convert to Spotify
            — populated only when no Spotify URL was found, so it's a
            true fallback signal.

        Both fields can be None when MB has no usable URL relationships
        or when the request fails (caller treats failures as soft misses
        and falls back further).
        """
        await _MB_BUCKET.acquire()
        try:
            r = await self._http.get(
                f"/recording/{mbid}",
                params={"inc": "url-rels", "fmt": "json"},
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError):
            return MBResolution(None, None)

        if r.status_code != 200:
            return MBResolution(None, None)

        try:
            data = r.json()
        except ValueError:
            return MBResolution(None, None)

        relations = data.get("relations") or []
        spotify_id: str | None = None
        # Prefer URLs from streaming relationships, but if none of those
        # match scan all URL relationships — we've seen Spotify links
        # filed under broader 'other databases' relationship types.
        for rel in relations:
            url = (rel.get("url") or {}).get("resource", "")
            if not url:
                continue
            rel_type = (rel.get("type") or "").lower()
            m = _SPOTIFY_TRACK_URL_RE.search(url)
            if m and (rel_type in _STREAMING_REL_TYPES or "spotify" in rel_type or rel_type == "other databases"):
                spotify_id = m.group(1)
                break
        if spotify_id is None:
            # Last-ditch sweep: any Spotify track URL anywhere on the recording.
            for rel in relations:
                url = (rel.get("url") or {}).get("resource", "")
                m = _SPOTIFY_TRACK_URL_RE.search(url)
                if m:
                    spotify_id = m.group(1)
                    break

        if spotify_id is not None:
            # No need to look for Odesli-resolvable URLs — we've already
            # got the answer.
            return MBResolution(spotify_id, None)

        # No Spotify URL on this recording. Look for any other streaming
        # platform URL Odesli can convert. Iterate hosts in preference
        # order so the first hit wins.
        for host in _ODESLI_RESOLVABLE_HOSTS:
            for rel in relations:
                url = (rel.get("url") or {}).get("resource", "")
                if host in url:
                    return MBResolution(None, url)
        return MBResolution(None, None)

    async def spotify_track_id_for_mbid(self, mbid: str) -> str | None:
        """Back-compat shim: returns just the Spotify track ID, if any.

        New callers should use `resolve_mbid` instead — it surfaces the
        fallback URL too in a single round-trip.
        """
        return (await self.resolve_mbid(mbid)).spotify_track_id


@dataclass(slots=True, frozen=True)
class MBResolution:
    spotify_track_id: str | None
    odesli_url: str | None
