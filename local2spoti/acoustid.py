from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx
import orjson


def fpcalc_available() -> bool:
    return shutil.which("fpcalc") is not None


@dataclass(slots=True)
class AcoustidMatch:
    artist: str
    title: str
    score: float


async def fingerprint(path: Path) -> tuple[int, str] | None:
    """Run fpcalc and return (duration_seconds, fingerprint) or None on failure."""
    if not fpcalc_available():
        return None
    proc = await asyncio.create_subprocess_exec(
        "fpcalc", "-json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    data = orjson.loads(out)
    return int(data["duration"]), data["fingerprint"]


class AcoustidClient:
    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def lookup(self, *, fingerprint: str, duration: int) -> AcoustidMatch | None:
        r = await self._http.get(
            "https://api.acoustid.org/v2/lookup",
            params={
                "client": self._api_key,
                "duration": duration,
                "fingerprint": fingerprint,
                "meta": "recordings",
                "format": "json",
            },
        )
        if r.status_code != 200:
            return None
        data = r.json()
        for result in data.get("results", []):
            for rec in result.get("recordings") or []:
                artists = rec.get("artists") or []
                title = rec.get("title")
                if artists and title:
                    return AcoustidMatch(
                        artist=artists[0].get("name", ""),
                        title=title,
                        score=result.get("score", 0.0),
                    )
        return None
