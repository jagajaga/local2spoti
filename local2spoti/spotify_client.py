from __future__ import annotations

import asyncio
from typing import Any

import httpx
import orjson

from .ratelimit import TokenBucket

_BASE = "https://api.spotify.com/v1"


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
            r = await self._http.request(
                method, path,
                params=params,
                content=content,
                headers={"Content-Type": "application/json"} if json is not None else None,
            )
            if r.status_code == 429:
                # Spotify can return huge Retry-After values when seriously
                # throttled — we've observed 60_000+ s (≈17h). Cap the
                # local pause at 5 minutes. If they're still angry after
                # 5 min we'll just take another 429 and pause again, but
                # at worst that's one probe per 5 min instead of sitting
                # idle for a full day. The pipeline-level heartbeat keeps
                # showing the user what's happening.
                raw_wait = float(r.headers.get("Retry-After", "1"))
                wait = min(raw_wait, 300.0)
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
