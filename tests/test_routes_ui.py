from datetime import UTC, datetime

from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
import pytest
from bs4 import BeautifulSoup

from local2spoti.main import create_app
from local2spoti.db import connect, init_schema
from local2spoti.models import FileStatus, LocalFile
from local2spoti import repo


async def test_dashboard_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/dashboard")
    assert r.status_code == 200
    soup = BeautifulSoup(r.text, "html.parser")
    assert soup.find("h2", string=lambda s: s and "Library" in s)
    assert soup.find("h2", string=lambda s: s and "Spotify" in s)


async def test_files_page_lists_matched(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        await repo.upsert_local_file(conn, LocalFile(
            path="/a.mp3", mtime=1, size=1, format="mp3",
            artist="Daft Punk", title="X", status=FileStatus.MATCHED,
            spotify_track_id="t1",
        ), now=datetime(2026, 5, 4, tzinfo=UTC))
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/files?status=matched")
    assert r.status_code == 200
    assert "Daft Punk" in r.text


async def test_files_status_review_redirects_to_review(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/files?status=review", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/review"


async def test_files_status_unmatched_redirects_to_unmatched(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/files?status=unmatched", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/unmatched"
