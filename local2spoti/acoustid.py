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
    # MusicBrainz Recording ID for the matched track, when AcoustID gave
    # us one. Used downstream to resolve Spotify track URLs via MB's URL
    # relationships (bypasses /v1/search entirely for tracks MB knows).
    recording_id: str | None = None


class AcoustidError(Exception):
    """Raised when the AcoustID API returns a structured error.

    Common cases:
      - code 4: invalid API key
      - code 6: server too busy
      - code 8: not allowed
    """

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"AcoustID error {code}: {message}")


async def fingerprint(path: Path, *, timeout: float = 30.0) -> tuple[int, str] | None:
    """Run fpcalc and return (duration_seconds, fingerprint) or None on failure.

    Bounded by `timeout` (default 30s). On a slow/corrupt file or a USB
    drive that's gone unresponsive, fpcalc can hang indefinitely while
    trying to read the audio stream — without a timeout the entire
    deep-scan loop appears 'stuck' on whichever file got unlucky.
    On timeout we kill the subprocess and return None (caller treats it
    as fpcalc_failed and moves on).
    """
    if not fpcalc_available():
        return None
    proc = await asyncio.create_subprocess_exec(
        "fpcalc", "-json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return None
    if proc.returncode != 0:
        return None
    try:
        data = orjson.loads(out)
        return int(data["duration"]), data["fingerprint"]
    except (ValueError, KeyError):
        return None


class AcoustidClient:
    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def lookup(self, *, fingerprint: str, duration: int) -> AcoustidMatch | None:
        """Look up a fingerprint. Returns None when AcoustID has no match.

        Raises AcoustidError when the *API* returns a structured error
        (invalid key, rate limit, etc.) — distinct from a successful "no
        match" response. The HTTP status is 200 in both cases; the
        difference lives in the JSON `status` field.
        """
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
            raise AcoustidError(
                code=-1, message=f"HTTP {r.status_code} {r.text[:200]}",
            )
        data = r.json()
        if data.get("status") == "error":
            err = data.get("error") or {}
            raise AcoustidError(
                code=int(err.get("code", -1)),
                message=str(err.get("message", "unknown error")),
            )
        for result in data.get("results", []):
            for rec in result.get("recordings") or []:
                artists = rec.get("artists") or []
                title = rec.get("title")
                if artists and title:
                    return AcoustidMatch(
                        artist=artists[0].get("name", ""),
                        title=title,
                        score=result.get("score", 0.0),
                        recording_id=rec.get("id"),
                    )
        return None
