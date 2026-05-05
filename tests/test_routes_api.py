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


async def test_deep_scan_status_review_only_processes_review_files(tmp_path, monkeypatch):
    """The /review page passes ?status=review; verify the endpoint
    queries the correct pool. We don't actually call AcoustID — the test
    just verifies the SQL filter is applied via row count."""
    from local2spoti.recovery import deep_scan_unmatched
    from datetime import UTC, datetime as _dt

    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        now = _dt(2026, 5, 4, tzinfo=UTC)
        await repo.upsert_local_file(conn, LocalFile(
            path="/r.mp3", mtime=1, size=1, format="mp3",
            artist="A", title="T", status=FileStatus.REVIEW,
        ), now=now)
        await repo.upsert_local_file(conn, LocalFile(
            path="/u.mp3", mtime=1, size=1, format="mp3",
            artist="B", title="T", status=FileStatus.UNMATCHED,
        ), now=now)

    # Fake state with bus we capture; no real AcoustID call (no fpcalc here)
    from local2spoti.state import AppState
    from local2spoti.config import Settings
    settings = Settings(spotify_client_id="x", acoustid_api_key="bogus")
    captured: list = []
    class _Bus:
        async def publish(self, e): captured.append(e)
        async def flush(self): pass
    state = AppState(settings=settings)
    state.bus = _Bus()  # type: ignore[assignment]
    import asyncio
    state.cancel_event = asyncio.Event()
    async with connect(db) as conn:
        state.db_conn = conn
        # status=review: should only consider the 1 review file
        await deep_scan_unmatched(state, status="review")
    # First emitted event should mention 1 file (only the review one)
    fingerprinting_msgs = [e for e in captured if "fingerprint" in (e.message or "")]
    assert any("1 review files" in (e.message or "") for e in fingerprinting_msgs)


async def test_retry_errors_resets_files_and_clears_candidates(tmp_path, monkeypatch):
    """Reset files in error status back to scanned + drop their candidates."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        now = datetime(2026, 5, 4, tzinfo=UTC)
        # 2 error files (with stale candidates) + 1 unrelated matched file
        await repo.upsert_local_file(conn, LocalFile(
            path="/e1.mp3", mtime=1, size=1, format="mp3",
            artist="A", title="T1", status=FileStatus.ERROR,
            last_error="403 GET /search",
        ), now=now)
        await repo.upsert_local_file(conn, LocalFile(
            path="/e2.mp3", mtime=1, size=1, format="mp3",
            artist="A", title="T2", status=FileStatus.ERROR,
            last_error="connection failed",
        ), now=now)
        await repo.upsert_local_file(conn, LocalFile(
            path="/m.mp3", mtime=1, size=1, format="mp3",
            artist="X", title="Y", status=FileStatus.MATCHED,
        ), now=now)
        # Stale candidate on one of the error files
        cur = await conn.execute("SELECT id FROM local_file WHERE path='/e1.mp3'")
        (e1_id,) = await cur.fetchone()
        await repo.insert_candidates(conn, e1_id, [
            MatchCandidate(spotify_track_id="t", spotify_artist="X",
                           spotify_title="Y", artist_similarity=0.5,
                           title_similarity=0.5, confidence=0.5, rank=1),
        ], now=now)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/retry_errors")
    assert r.status_code == 200
    assert r.json()["retried"] == 2

    async with connect(db) as conn:
        cur = await conn.execute(
            "SELECT path, status, last_error FROM local_file ORDER BY path"
        )
        rows = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
        cur = await conn.execute("SELECT COUNT(*) FROM match_candidate")
        candidate_count = (await cur.fetchone())[0]
    assert rows["/e1.mp3"] == ("scanned", None)
    assert rows["/e2.mp3"] == ("scanned", None)
    assert rows["/m.mp3"] == ("matched", None)  # untouched
    assert candidate_count == 0  # stale candidate cleared


async def test_retry_one_error_resets_single_file(tmp_path, monkeypatch):
    """POST /api/retry_error/{id} resets just that file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        now = datetime(2026, 5, 4, tzinfo=UTC)
        await repo.upsert_local_file(conn, LocalFile(
            path="/e1.mp3", mtime=1, size=1, format="mp3",
            artist="A", title="T1", status=FileStatus.ERROR,
        ), now=now)
        await repo.upsert_local_file(conn, LocalFile(
            path="/e2.mp3", mtime=1, size=1, format="mp3",
            artist="A", title="T2", status=FileStatus.ERROR,
        ), now=now)
        # set_status is the path that actually persists last_error (matches
        # how the pipeline records errors).
        await conn.execute(
            "UPDATE local_file SET last_error='boom' WHERE path IN ('/e1.mp3', '/e2.mp3')"
        )
        await conn.commit()

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # retry just file id 1; file id 2 stays in error with its message
            r = await c.post("/api/retry_error/1")
    assert r.status_code == 200
    async with connect(db) as conn:
        cur = await conn.execute(
            "SELECT path, status, last_error FROM local_file ORDER BY path"
        )
        rows = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
    assert rows["/e1.mp3"] == ("scanned", None)
    assert rows["/e2.mp3"] == ("error", "boom")


async def test_retry_one_error_rejects_non_error_file(tmp_path, monkeypatch):
    """Can't retry a file that isn't in error status — returns 400."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        now = datetime(2026, 5, 4, tzinfo=UTC)
        await repo.upsert_local_file(conn, LocalFile(
            path="/m.mp3", mtime=1, size=1, format="mp3",
            artist="A", title="T", status=FileStatus.MATCHED,
        ), now=now)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/retry_error/1")
    assert r.status_code == 400
    assert "not in error status" in r.json()["error"]


async def test_logout_clears_spotify_token(tmp_path, monkeypatch):
    """POST /api/logout drops the stored Spotify token; re-login at
    /auth/login afterwards is the expected path."""
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
            r = await c.post("/api/logout")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    async with connect(db) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM auth_token WHERE key='spotify'")
        assert (await cur.fetchone())[0] == 0


async def test_logout_blocked_while_job_running(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)

    app = create_app()
    async with LifespanManager(app):
        # Plant a fake "running" scan task so logout refuses.
        async def _block(): await asyncio.sleep(60)
        app.state.app_state.scan_task = asyncio.create_task(_block())
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/api/logout")
        finally:
            app.state.app_state.scan_task.cancel()
    assert r.status_code == 409
    assert "stop running jobs" in r.json()["error"].lower()
