from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from local2spoti import repo
from local2spoti.db import connect, init_schema
from local2spoti.main import create_app
from local2spoti.models import FileStatus, LocalFile


async def test_push_endpoint_returns_count(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    async with connect(db) as conn:
        await init_schema(conn)
        await conn.execute(
            """INSERT INTO auth_token (key, access_token, refresh_token,
                                       expires_at, scope, user_id)
               VALUES ('spotify','at','rt','2099-01-01T00:00:00','x','user1')"""
        )
        await conn.commit()
        await repo.upsert_local_file(
            conn,
            LocalFile(
                path="/x.mp3",
                mtime=1,
                size=1,
                format="mp3",
                artist="A",
                title="T",
                spotify_track_id="t1",
                status=FileStatus.MATCHED,
            ),
            now=datetime(2026, 5, 4, tzinfo=UTC),
        )

    fake_client = AsyncMock()
    fake_client.me.return_value = {"id": "user1"}
    fake_client.create_playlist.return_value = {"id": "p1"}
    fake_client.add_tracks.return_value = None

    with patch("local2spoti.routes.api.SpotifyClient", return_value=fake_client):
        app = create_app()
        async with LifespanManager(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/api/push")
    assert r.status_code == 200
    assert r.json()["added"] == 1
