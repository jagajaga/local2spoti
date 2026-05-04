from datetime import UTC, datetime

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from local2spoti import repo
from local2spoti.db import connect, init_schema
from local2spoti.main import create_app
from local2spoti.models import FileStatus, LocalFile


async def _seed_some_state(db_path):
    async with connect(db_path) as conn:
        await init_schema(conn)
        now = datetime(2026, 5, 4, tzinfo=UTC)
        await repo.upsert_local_file(conn, LocalFile(
            path="/a.mp3", mtime=1, size=1, format="mp3",
            artist="A", title="T", spotify_track_id="t1",
            status=FileStatus.MATCHED,
        ), now=now)
        # Spotify auth + settings should survive reset
        await conn.execute(
            """INSERT INTO auth_token (key, access_token, refresh_token,
               expires_at, scope, user_id)
               VALUES ('spotify','at','rt','2099-01-01T00:00:00','x','user1')"""
        )
        await conn.execute(
            "INSERT INTO setting (key, value) VALUES ('threshold', 'strict')"
        )
        await conn.commit()


async def test_reset_wipes_files_keeps_auth_and_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True)
    await _seed_some_state(db)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/reset")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    async with connect(db) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM local_file")
        assert (await cur.fetchone())[0] == 0
        cur = await conn.execute("SELECT COUNT(*) FROM scan_run")
        assert (await cur.fetchone())[0] == 0
        # Auth and settings survive
        cur = await conn.execute("SELECT COUNT(*) FROM auth_token")
        assert (await cur.fetchone())[0] == 1
        cur = await conn.execute("SELECT value FROM setting WHERE key='threshold'")
        assert (await cur.fetchone())[0] == "strict"


async def test_reset_blocked_while_scan_running(tmp_path, monkeypatch):
    """Reset must refuse if a scan is in progress to avoid data corruption."""
    import asyncio
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True)
    await _seed_some_state(db)

    app = create_app()
    async with LifespanManager(app):
        # Manually plant a fake "running" scan task
        async def _block():
            await asyncio.sleep(60)
        app.state.app_state.scan_task = asyncio.create_task(_block())
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/api/reset")
        finally:
            app.state.app_state.scan_task.cancel()
    assert r.status_code == 409
    assert "running" in r.json()["error"]


async def test_jobs_have_independent_slots(tmp_path, monkeypatch):
    """ai_scan and deep_scan should use separate task slots so they can
    run in parallel — gating only on their own kind."""
    import asyncio

    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)

    app = create_app()
    async with LifespanManager(app):
        # Plant a fake long-running deep_scan; ai_scan should NOT be blocked.
        async def _block():
            await asyncio.sleep(60)
        app.state.app_state.deep_scan_task = asyncio.create_task(_block())
        try:
            # any_job_running should still see something
            assert app.state.app_state.any_job_running()
            # But the slots are independent
            assert app.state.app_state.scan_task is None
            assert app.state.app_state.ai_scan_task is None
        finally:
            app.state.app_state.deep_scan_task.cancel()
