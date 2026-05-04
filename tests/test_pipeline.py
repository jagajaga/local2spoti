from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from local2spoti.db import connect, init_schema
from local2spoti.events import EventBus
from local2spoti.matcher import Threshold
from local2spoti.models import FileStatus
from local2spoti.pipeline import run_scan
from local2spoti import repo


@pytest.fixture
def fake_client():
    c = AsyncMock()
    c.search_artist.return_value = {"id": "art1", "name": "Daft Punk"}
    c.artist_albums.return_value = [{"id": "alb1", "name": "Homework"}]
    c.albums_batch.return_value = [{
        "id": "alb1", "name": "Homework", "tracks": {"items": [
            {"id": "t1", "name": "Around the World", "duration_ms": 423000,
             "artists": [{"name": "Daft Punk"}]},
        ]},
    }]
    return c


async def test_scan_e2e(tmp_path, fake_client):
    import shutil, subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    library = tmp_path / "lib"
    (library / "Daft Punk").mkdir(parents=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", "1", "-q:a", "9",
        "-metadata", "artist=Daft Punk",
        "-metadata", "title=Around the World",
        "-metadata", "album=Homework",
        str(library / "Daft Punk" / "01 - Around the World.mp3"),
    ], check=True, capture_output=True)

    db_path = tmp_path / "state.db"
    bus = EventBus(min_interval=0.0)
    async with connect(db_path) as conn:
        await init_schema(conn)
        result = await run_scan(
            conn=conn, client=fake_client, library_root=library,
            threshold=Threshold.BALANCED, bus=bus,
        )
        assert result.matched >= 1
        counts = await repo.count_by_status(conn)
        assert counts.get(FileStatus.MATCHED, 0) >= 1


async def test_scan_resumability(tmp_path, fake_client):
    import shutil, subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    library = tmp_path / "lib"
    (library / "Daft Punk").mkdir(parents=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", "1", "-q:a", "9",
        "-metadata", "artist=Daft Punk",
        "-metadata", "title=Around the World",
        str(library / "Daft Punk" / "01 - Around the World.mp3"),
    ], check=True, capture_output=True)

    db_path = tmp_path / "state.db"
    async with connect(db_path) as conn:
        await init_schema(conn)
        await run_scan(conn=conn, client=fake_client, library_root=library,
                       threshold=Threshold.BALANCED, bus=EventBus(min_interval=0.0))
        result2 = await run_scan(conn=conn, client=fake_client, library_root=library,
                                  threshold=Threshold.BALANCED, bus=EventBus(min_interval=0.0))
    assert result2.processed_files == 0


from unittest.mock import AsyncMock as _AsyncMock
from local2spoti.events import EventBus as _EventBus
from local2spoti.matcher import Threshold as _Threshold
from local2spoti.pipeline import _stage_match as __stage_match
from local2spoti import repo as _repo
from local2spoti.spotify_client import SpotifyError


async def test_match_stage_isolates_per_artist_failures(tmp_path):
    """Regression: one artist returning Spotify 403 used to kill the
    entire match stage. Now it should only error that artist's files."""
    from local2spoti.db import connect as _connect, init_schema as _init
    from datetime import UTC as _UTC, datetime as _dt
    from local2spoti.models import FileStatus as _FS, LocalFile as _LF

    db = tmp_path / "iso.db"
    async with _connect(db) as conn:
        await _init(conn)
        now = _dt(2026, 5, 4, tzinfo=_UTC)
        await _repo.upsert_local_file(conn, _LF(
            path="/bad.mp3", mtime=1, size=1, format="mp3",
            artist="GeoBlocked", title="X", status=_FS.SCANNED,
        ), now=now)
        await _repo.upsert_local_file(conn, _LF(
            path="/good.mp3", mtime=1, size=1, format="mp3",
            artist="DaftPunk", title="Around the World",
            duration_ms=423000, status=_FS.SCANNED,
        ), now=now)

        client = _AsyncMock()
        async def _search(name):
            if name == "GeoBlocked":
                raise SpotifyError("403 GET /search: Spotify is unavailable in this country")
            return {"id": "art1", "name": name}
        client.search_artist.side_effect = _search
        client.artist_albums.return_value = [{"id": "alb1", "name": "Homework"}]
        client.albums_batch.return_value = [{
            "id": "alb1", "name": "Homework", "tracks": {"items": [
                {"id": "t1", "name": "Around the World", "duration_ms": 423000,
                 "artists": [{"name": "DaftPunk"}]},
            ]},
        }]

        result = await __stage_match(
            conn, client, _Threshold.BALANCED,
            bus=_EventBus(min_interval=0.0), now=now,
        )
        assert result["errors"] == 1
        assert (result["matched"] + result["review"]) >= 1
        cur = await conn.execute(
            "SELECT path, status FROM local_file ORDER BY path"
        )
        rows = {r[0]: r[1] for r in await cur.fetchall()}
        assert rows["/bad.mp3"] == "error"
        assert rows["/good.mp3"] in ("matched", "review")
