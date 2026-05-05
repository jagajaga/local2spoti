from __future__ import annotations

import asyncio
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
    ) -> None:
        self._token = access_token
        self._bucket = bucket
        self._http = httpx.AsyncClient(
            base_url=_BASE,
            timeout=timeout,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def set_access_token(self, token: str) -> None:
        self._token = token
        self._http.headers["Authorization"] = f"Bearer {token}"

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
        while True:
            await self._bucket.acquire()
            content = orjson.dumps(json) if json is not None else None
            try:
                r = await self._http.request(
                    method, path,
                    params=params,
                    content=content,
                    headers={"Content-Type": "application/json"} if json is not None else None,
                )
            except (
                httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError,
            ):
                # Network blip — DNS hiccup, captive portal, brief WiFi
                # drop, Spotify took too long to answer. Definitionally
                # transient. Pause and retry forever; the user's only
                # escape hatch is the Stop button (cancel_event in the
                # pipeline). Same 60s default the 429 path uses when
                # there's no Retry-After header — keeps a single
                # consistent "retry-after-this-long" knob. Without this
                # an httpx.ConnectError would propagate to process_artist
                # and the group's files would get marked as error.
                self._bucket.pause_for(_DEFAULT_RATE_LIMIT_PAUSE_SECONDS)
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
        self, artist: str, title: str, *, limit: int = 5,
    ) -> list[dict[str, Any]]:
        q = f'track:"{title}" artist:"{artist}"'
        data = await self._get("/search", q=q, type="track", limit=limit)
        return data.get("tracks", {}).get("items", [])

    async def search_artist(self, name: str) -> dict[str, Any] | None:
        data = await self._get("/search", q=name, type="artist", limit=1)
        items = data.get("artists", {}).get("items", [])
        return items[0] if items else None

    async def artist_albums(self, artist_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            data = await self._get(
                f"/artists/{artist_id}/albums",
                include_groups="album,single,compilation",
                limit=50, offset=offset,
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
