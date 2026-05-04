from datetime import UTC, datetime
import pytest
import pytest_asyncio

from local2spoti.db import connect, init_schema
from local2spoti.models import FileStatus, LocalFile
from local2spoti import repo


@pytest_asyncio.fixture
async def conn(tmp_path):
    async with connect(tmp_path / "t.db") as c:
        await init_schema(c)
        yield c


async def test_upsert_new_file(conn):
    f = LocalFile(path="/a.mp3", mtime=10, size=100, format="mp3")
    fid = await repo.upsert_local_file(conn, f, now=datetime(2026, 5, 4, tzinfo=UTC))
    assert fid is not False
    got = await repo.get_local_file_by_path(conn, "/a.mp3")
    assert got.status == FileStatus.NEW
    assert got.first_seen_at is not None


async def test_upsert_unchanged_skips(conn):
    now = datetime(2026, 5, 4, tzinfo=UTC)
    f = LocalFile(path="/a.mp3", mtime=10, size=100, format="mp3")
    await repo.upsert_local_file(conn, f, now=now)
    changed = await repo.upsert_local_file(conn, f, now=now)
    assert changed is False


async def test_change_detected_when_mtime_changes(conn):
    now = datetime(2026, 5, 4, tzinfo=UTC)
    f = LocalFile(path="/a.mp3", mtime=10, size=100, format="mp3")
    await repo.upsert_local_file(conn, f, now=now)
    f.mtime = 20
    changed = await repo.upsert_local_file(conn, f, now=now)
    assert changed is True
    got = await repo.get_local_file_by_path(conn, "/a.mp3")
    assert got.status == FileStatus.NEW


async def test_count_by_status(conn):
    now = datetime(2026, 5, 4, tzinfo=UTC)
    for i in range(3):
        await repo.upsert_local_file(
            conn,
            LocalFile(path=f"/{i}.mp3", mtime=i, size=1, format="mp3",
                      status=FileStatus.MATCHED if i < 2 else FileStatus.REVIEW),
            now=now,
        )
    counts = await repo.count_by_status(conn)
    assert counts[FileStatus.MATCHED] == 2
    assert counts[FileStatus.REVIEW] == 1


async def test_mark_missing(conn):
    now = datetime(2026, 5, 4, tzinfo=UTC)
    later = datetime(2026, 5, 5, tzinfo=UTC)
    await repo.upsert_local_file(conn,
        LocalFile(path="/seen.mp3", mtime=1, size=1, format="mp3",
                  status=FileStatus.MATCHED), now=now)
    await repo.upsert_local_file(conn,
        LocalFile(path="/gone.mp3", mtime=1, size=1, format="mp3",
                  status=FileStatus.MATCHED), now=now)
    await repo.touch_last_scanned(conn, "/seen.mp3", later)
    n = await repo.mark_missing_files(conn, scan_started=later)
    assert n == 1
    gone = await repo.get_local_file_by_path(conn, "/gone.mp3")
    assert gone.status == FileStatus.MISSING


from local2spoti.models import MatchCandidate as MC


async def test_clear_candidates_removes_only_target_file_rows(conn):
    now = datetime(2026, 5, 4, tzinfo=UTC)
    await repo.upsert_local_file(conn, LocalFile(
        path="/a.mp3", mtime=1, size=1, format="mp3",
        artist="A", title="T", status=FileStatus.REVIEW,
    ), now=now)
    await repo.upsert_local_file(conn, LocalFile(
        path="/b.mp3", mtime=1, size=1, format="mp3",
        artist="B", title="T", status=FileStatus.REVIEW,
    ), now=now)
    cur = await conn.execute("SELECT id FROM local_file ORDER BY id")
    a_id, b_id = [r[0] for r in await cur.fetchall()]

    await repo.insert_candidates(conn, a_id, [
        MC(spotify_track_id="t1", spotify_artist="A", spotify_title="T",
           artist_similarity=0.9, title_similarity=0.9, confidence=0.9, rank=1),
    ], now=now)
    await repo.insert_candidates(conn, b_id, [
        MC(spotify_track_id="t2", spotify_artist="B", spotify_title="T",
           artist_similarity=0.9, title_similarity=0.9, confidence=0.9, rank=1),
    ], now=now)

    await repo.clear_candidates(conn, a_id)

    cur = await conn.execute(
        "SELECT local_file_id FROM match_candidate ORDER BY local_file_id"
    )
    remaining = [r[0] for r in await cur.fetchall()]
    assert remaining == [b_id]  # only B's candidate left
