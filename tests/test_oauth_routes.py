from httpx import AsyncClient, ASGITransport
import pytest
from asgi_lifespan import LifespanManager

from local2spoti.main import create_app


async def test_login_redirects_to_spotify(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCAL2SPOTI_SPOTIFY_CLIENT_ID", "test_client_id")
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/auth/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "accounts.spotify.com/authorize" in r.headers["location"]
    assert "client_id=test_client_id" in r.headers["location"]
