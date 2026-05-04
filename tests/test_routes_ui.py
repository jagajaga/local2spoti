from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
import pytest
from bs4 import BeautifulSoup

from local2spoti.main import create_app


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
