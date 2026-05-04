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
