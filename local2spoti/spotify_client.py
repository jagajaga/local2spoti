from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import orjson

from .ratelimit import TokenBucket

_BASE = "https://api.spotify.com/v1"

# Default pause when we know we should back off but Spotify didn't tell us
# how long (used for both no-Retry-After 429s/403s and httpx network
# errors). The 429 handler still respects an explicit Retry-After up to
# the cap below.
_DEFAULT_RATE_LIMIT_PAUSE_SECONDS = 60.0
# Hard cap on any pause Spotify asks for. They've been observed sending
# multi-hour Retry-After values; we'd rather probe every 5 min than sit
# idle for the rest of the day.
_MAX_RATE_LIMIT_PAUSE_SECONDS = 300.0

# Substrings in 403 response bodies that mean "soft rate limit, back off"
# rather than a real authorization failure. After Spotify 429s us a few
# times, follow-up requests start coming back as 403 with this message
# — they're still about throttling, not geoblocks for specific content.
_SOFT_RATE_LIMIT_403_HINTS = (
    "Spotify is unavailable in this country",
    "rate limit",
)


def _is_soft_rate_limit_403(response: httpx.Response) -> bool:
    if response.status_code != 403:
        return False
    body = response.text
    return any(hint.lower() in body.lower() for hint in _SOFT_RATE_LIMIT_403_HINTS)


class SpotifyError(Exception):
    pass


class SpotifyClient:
    def __init__(
        self,
        *,
        access_token: str,
        bucket: TokenBucket,
        timeout: float = 30.0,
        token_provider: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        """`token_provider`, when set, is awaited on 401 to fetch the
        latest token from the persistent store. Lets the client survive
        a long-running job that crosses a token-refresh boundary — the
        background refresh_loop updates the DB row, and we re-read it
        on the first 401 instead of failing every subsequent file."""
        self._token = access_token
        self._bucket = bucket
        self._token_provider = token_provider
        self._http = httpx.AsyncClient(
            base_url=_BASE,
            timeout=timeout,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self._refresh_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    def set_access_token(self, token: str) -> None:
        self._token = token
        self._http.headers["Authorization"] = f"Bearer {token}"

    async def _try_refresh_token(self) -> None:
        """Ask the token_provider for the latest access token and adopt
        it. Locked so a burst of concurrent 401s collapses into a single
        provider call — but every individual 401'd request still gets
        its own retry (the caller doesn't gate on whether *this* call
        was the one that performed the refresh; see _request)."""
        if self._token_provider is None:
            return
        async with self._refresh_lock:
            try:
                fresh = await self._token_provider()
            except Exception:
                return
            if fresh and fresh != self._token:
                self.set_access_token(fresh)

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self._request("GET", path, params=params or None)

    async def _post(self, path: str, *, json: Any | None = None) -> dict[str, Any]:
        return await self._request("POST", path, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> dict[str, Any]:
        attempt = 0
        auth_retried = False
        while True:
            await self._bucket.acquire()
            content = orjson.dumps(json) if json is not None else None
            try:
                r = await self._http.request(
                    method,
                    path,
                    params=params,
                    content=content,
                    headers={"Content-Type": "application/json"} if json is not None else None,
                )
            except (
                httpx.ConnectError,
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ):
                # Network blip — DNS hiccup, captive portal, brief WiFi
                # drop, Spotify took too long to answer, server closed
                # the connection mid-response. Definitionally transient.
                # Pause and retry forever; the user's only escape hatch
                # is the Stop button (cancel_event in the pipeline).
                # Same 60s default the 429 path uses when there's no
                # Retry-After header — keeps a single consistent
                # "retry-after-this-long" knob. Without this an
                # httpx.ConnectError / RemoteProtocolError would
                # propagate to process_artist and the group's files
                # would get marked as error.
                self._bucket.pause_for(_DEFAULT_RATE_LIMIT_PAUSE_SECONDS)
                continue
            # 401 mid-job almost always means the access token expired
            # and the token_provider can fetch / force-refresh a new one.
            # Retry exactly once per request — the per-request flag
            # prevents an infinite loop on a genuinely bad token, and
            # lets every concurrent worker get its own retry shot
            # (a shared "did we refresh?" check would race: worker A
            # refreshes, worker B sees the new token already in place
            # and would otherwise wrongly conclude refresh failed).
            if r.status_code == 401 and not auth_retried:
                auth_retried = True
                await self._try_refresh_token()
                continue
            # Treat both 429 and "Spotify is unavailable in this country"
            # 403s as soft rate-limit signals. Empirically, Spotify
            # escalates from 429 → 403-geoblock when our IP keeps hitting
            # /search after they've started throttling — the message is
            # misleading but the meaning is the same: "back off, try
            # later". Same handling: pause the bucket (capped at 5 min)
            # and retry the request. We do NOT raise — that would make
            # process_artist mark the file as 'error', but the file is
            # fine; only Spotify is being grumpy.
            if r.status_code == 429 or _is_soft_rate_limit_403(r):
                raw_wait = float(
                    r.headers.get("Retry-After", _DEFAULT_RATE_LIMIT_PAUSE_SECONDS),
                )
                wait = min(raw_wait, _MAX_RATE_LIMIT_PAUSE_SECONDS)
                self._bucket.pause_for(wait)
                continue
            if r.status_code >= 500 and attempt < 4:
                await asyncio.sleep(min(2**attempt, 8) + 0.1 * attempt)
                attempt += 1
                continue
            if r.status_code >= 400:
                raise SpotifyError(f"{r.status_code} {method} {path}: {r.text[:200]}")
            return orjson.loads(r.content) if r.content else {}

    async def search_tracks(
        self,
        artist: str,
        title: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        q = f'track:"{title}" artist:"{artist}"'
        data = await self._get("/search", q=q, type="track", limit=limit)
        return data.get("tracks", {}).get("items", [])

    async def search_track_by_isrc(self, isrc: str) -> dict[str, Any] | None:
        """Look up the Spotify track for an ISRC, deterministic & 1-result.

        ISRC is a global recording-id standard that Spotify indexes on, so
        `q=isrc:<code>` returns the exact track with no fuzzy matching.
        Returns the track dict on hit, None on miss/empty results.
        """
        data = await self._get(
            "/search",
            q=f"isrc:{isrc}",
            type="track",
            limit=1,
        )
        items = data.get("tracks", {}).get("items", [])
        return items[0] if items else None

    async def search_artist(self, name: str) -> dict[str, Any] | None:
        """Resolve an artist name to a Spotify artist record.

        Fetches the top 5 results and picks the one whose name actually
        matches what we asked for. Spotify's default `limit=1` is a trap:
        for classical / non-English / less-popular artists their
        relevance ranking often returns a *more* popular adjacent artist
        (Mozart → Beethoven, DJ Shadow → Massive Attack, etc.) because
        those artists appear on compilations that mention the searched
        name. We refuse anything below 0.75 fuzzy similarity — better a
        cache miss than a 47K-track wrong catalog cached for 30 days.
        """
        from .normalize import similarity

        data = await self._get("/search", q=name, type="artist", limit=5)
        items = data.get("artists", {}).get("items", [])
        if not items:
            return None
        best: dict[str, Any] | None = None
        best_sim = 0.0
        for it in items:
            sim = similarity(name, it.get("name", ""))
            if sim > best_sim:
                best_sim = sim
                best = it
        if best_sim < 0.75:
            return None
        return best

    async def artist_albums(self, artist_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            data = await self._get(
                f"/artists/{artist_id}/albums",
                include_groups="album,single,compilation",
                limit=50,
                offset=offset,
            )
            items = data.get("items", [])
            out.extend(items)
            if not data.get("next") or not items:
                return out
            offset += len(items)

    async def albums_batch(self, album_ids: list[str]) -> list[dict[str, Any]]:
        if not album_ids:
            return []
        out: list[dict[str, Any]] = []
        for i in range(0, len(album_ids), 20):
            chunk = album_ids[i : i + 20]
            data = await self._get("/albums", ids=",".join(chunk))
            out.extend(data.get("albums", []))
        return out

    async def create_playlist(self, user_id: str, name: str, *, public: bool = False) -> dict[str, Any]:
        return await self._post(
            f"/users/{user_id}/playlists",
            json={"name": name, "public": public},
        )

    async def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            await self._post(
                f"/playlists/{playlist_id}/tracks",
                json={"uris": uris[i : i + 100]},
            )

    async def me(self) -> dict[str, Any]:
        return await self._get("/me")
