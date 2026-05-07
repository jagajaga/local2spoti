from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import aiosqlite

from .spotify_oauth import refresh_token


async def refresh_if_expiring(
    *,
    conn: aiosqlite.Connection,
    client_id: str,
    threshold_seconds: int = 300,
) -> bool:
    """If the spotify token expires within `threshold_seconds`, refresh it."""
    cur = await conn.execute("SELECT refresh_token, expires_at FROM auth_token WHERE key='spotify'")
    row = await cur.fetchone()
    if not row:
        return False
    rtk, expires_at_iso = row
    expires_at = datetime.fromisoformat(expires_at_iso)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at - datetime.now(UTC) > timedelta(seconds=threshold_seconds):
        return False
    new = await refresh_token(refresh=rtk, client_id=client_id)
    new_expires = datetime.now(UTC) + timedelta(seconds=new["expires_in"] - 60)
    new_refresh = new.get("refresh_token", rtk)  # Spotify may rotate it
    await conn.execute(
        """UPDATE auth_token SET access_token=?, refresh_token=?,
           expires_at=?, scope=? WHERE key='spotify'""",
        (new["access_token"], new_refresh, new_expires.isoformat(), new["scope"]),
    )
    await conn.commit()
    return True


async def refresh_loop(
    *,
    conn: aiosqlite.Connection,
    client_id: str,
    interval_seconds: int = 60,
) -> None:
    """Run forever, checking token expiry every `interval_seconds`."""
    while True:
        try:
            if client_id:
                await refresh_if_expiring(conn=conn, client_id=client_id)
        except Exception:
            pass
        await asyncio.sleep(interval_seconds)
