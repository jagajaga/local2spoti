from datetime import UTC, datetime
from httpx import AsyncClient, ASGITransport
import pytest
from asgi_lifespan import LifespanManager

from local2spoti.db import connect, init_schema
from local2spoti.main import create_app
from local2spoti.models import FileStatus, LocalFile, MatchCandidate
from local2spoti import repo


async def _seed_review(tmp_path):
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        now = datetime(2026, 5, 4, tzinfo=UTC)
        for i in range(2):
            await repo.upsert_local_file(conn, LocalFile(
                path=f"/{i}.mp3", mtime=1, size=1, format="mp3",
                artist="Daft Punk", title=f"Track {i}",
                status=FileStatus.REVIEW,
            ), now=now)
            cur = await conn.execute("SELECT id FROM local_file WHERE path=?", (f"/{i}.mp3",))
            (fid,) = await cur.fetchone()
            await repo.insert_candidates(conn, fid, [
                MatchCandidate(spotify_track_id=f"top{i}", spotify_artist="Daft Punk",
                               spotify_title=f"Track {i}", artist_similarity=0.93,
                               title_similarity=0.93, confidence=0.92, rank=1),
                MatchCandidate(spotify_track_id=f"alt{i}", spotify_artist="Daft Punk",
                               spotify_title=f"Track {i} (Live)", artist_similarity=0.93,
                               title_similarity=0.85, confidence=0.85, rank=2),
            ], now=now)


async def test_review_lists_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    await _seed_review(tmp_path)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/review")
    assert r.status_code == 200
    assert "Track 0" in r.text
    assert "Track 1" in r.text


async def test_bulk_approve_top(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    await _seed_review(tmp_path)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/review/approve_top_visible", data={"file_ids": "1,2"})
    assert r.status_code == 200
    db = tmp_path / ".local2spoti" / "state.db"
    async with connect(db) as conn:
        cur = await conn.execute("SELECT status, spotify_track_id FROM local_file WHERE id=1")
        st, tid = await cur.fetchone()
    assert st == "matched"
    assert tid == "top0"
