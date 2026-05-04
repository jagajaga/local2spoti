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


from local2spoti.models import MatchCandidate


async def test_approve_above_confidence_picks_only_files_at_or_above_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        now = datetime(2026, 5, 4, tzinfo=UTC)
        # 3 review-status files with rank-1 confidences 0.95, 0.80, 0.55
        confidences = [0.95, 0.80, 0.55]
        for i, conf in enumerate(confidences):
            await repo.upsert_local_file(conn, LocalFile(
                path=f"/{i}.mp3", mtime=1, size=1, format="mp3",
                artist="A", title=f"T{i}", status=FileStatus.REVIEW,
            ), now=now)
            cur = await conn.execute("SELECT id FROM local_file WHERE path=?", (f"/{i}.mp3",))
            (fid,) = await cur.fetchone()
            await repo.insert_candidates(conn, fid, [
                MatchCandidate(spotify_track_id=f"top{i}", spotify_artist="A",
                               spotify_title=f"T{i}", artist_similarity=conf,
                               title_similarity=conf, confidence=conf, rank=1),
            ], now=now)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # Threshold 0.85 → only the 0.95 file qualifies
            r = await c.post("/api/review/approve_above_confidence",
                              data={"threshold": "0.85"})
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] == 1
    assert body["threshold"] == 0.85

    async with connect(db) as conn:
        cur = await conn.execute(
            "SELECT id, status FROM local_file ORDER BY id"
        )
        rows = await cur.fetchall()
    # Only file 1 (confidence 0.95) was approved → 'matched'
    statuses = {fid: status for fid, status in rows}
    assert statuses[1] == "matched"
    assert statuses[2] == "review"
    assert statuses[3] == "review"


async def test_approve_above_confidence_rejects_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/review/approve_above_confidence",
                              data={"threshold": "1.5"})
    assert r.status_code == 400
    assert "between 0.0 and 1.0" in r.json()["error"]


async def test_match_endpoint_requires_spotify(tmp_path, monkeypatch):
    """/api/match returns 400 if Spotify isn't connected."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/match")
    assert r.status_code == 400
    assert "Spotify" in r.json()["error"]


async def test_match_endpoint_starts_in_scan_slot(tmp_path, monkeypatch):
    """/api/match starts a background task in the scan_task slot.

    We seed a Spotify token but no scanned files — _stage_match emits a
    'nothing to match' event and returns instantly, which lets us assert
    the slot was used without standing up a real Spotify mock.
    """
    import asyncio

    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        await conn.execute(
            """INSERT INTO auth_token (key, access_token, refresh_token,
                                       expires_at, scope, user_id)
               VALUES ('spotify','at','rt','2099-01-01T00:00:00','x','u')"""
        )
        await conn.commit()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/match")
            assert r.status_code == 200
            assert r.json()["ok"] is True
            task = app.state.app_state.scan_task
            assert task is not None
            await asyncio.wait_for(task, timeout=5.0)
