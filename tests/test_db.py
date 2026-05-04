import asyncio
from pathlib import Path

import pytest

from local2spoti.db import connect, init_schema


@pytest.mark.asyncio
async def test_schema_creates_all_tables(tmp_path):
    db = tmp_path / "test.db"
    async with connect(db) as conn:
        await init_schema(conn)
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = [r[0] for r in await cur.fetchall()]
    assert rows == [
        "auth_token",
        "local_file",
        "match_candidate",
        "playlist",
        "playlist_track",
        "scan_run",
        "setting",
    ]


@pytest.mark.asyncio
async def test_pragmas_applied(tmp_path):
    db = tmp_path / "test.db"
    async with connect(db) as conn:
        cur = await conn.execute("PRAGMA journal_mode")
        mode = (await cur.fetchone())[0]
        cur = await conn.execute("PRAGMA foreign_keys")
        fk = (await cur.fetchone())[0]
    assert mode.lower() == "wal"
    assert fk == 1


@pytest.mark.asyncio
async def test_init_schema_idempotent(tmp_path):
    db = tmp_path / "test.db"
    async with connect(db) as conn:
        await init_schema(conn)
        await init_schema(conn)  # second run must not raise
