from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from local2spoti.main import create_app


async def test_set_library_root_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    library = tmp_path / "library"
    library.mkdir()
    app = create_app()
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/library", data={"path": str(library)})
    assert r.status_code == 200
    assert r.json()["library_root"] == str(library)


async def test_invalid_path_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/library", data={"path": "/does/not/exist"})
    assert r.status_code == 400
