"""Tests for the Claude-backed metadata identification path.

We mock the Anthropic API at the HTTP layer with respx so the real key never
hits the network during tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from local2spoti import repo
from local2spoti.ai_match import AIClient, Suggestion
from local2spoti.db import connect, init_schema
from local2spoti.main import create_app
from local2spoti.models import FileStatus, LocalFile


def _claude_response(items: list[dict]) -> dict:
    """Build a fake Anthropic /v1/messages response containing a JSON-schema text block."""
    import orjson
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": orjson.dumps({"items": items}).decode()}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 200},
    }


@respx.mock
async def test_aiclient_suggests_metadata_high_confidence(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=_claude_response([
            {"id": 1, "artist": "Daft Punk", "title": "Around the World",
             "album": "Homework", "confidence": "high",
             "reasoning": "filename clearly matches"},
        ]))
    )
    client = AIClient(api_key="test-key", model="claude-opus-4-7")
    try:
        out = await client.suggest_metadata([
            {"id": 1, "path": "/m/Daft Punk/Homework/Around the World.mp3",
             "artist": None, "title": None, "album": None},
        ])
    finally:
        await client.aclose()

    assert len(out) == 1
    assert out[0].file_id == 1
    assert out[0].artist == "Daft Punk"
    assert out[0].title == "Around the World"
    assert out[0].confidence == "high"
    assert out[0].usable is True


@respx.mock
async def test_aiclient_handles_unparseable_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=_claude_response([
            {"id": 5, "artist": None, "title": None, "album": None,
             "confidence": "none", "reasoning": "random hex filename"},
        ]))
    )
    client = AIClient(api_key="test-key")
    try:
        out = await client.suggest_metadata([
            {"id": 5, "path": "/m/8a3f0c.mp3", "artist": None, "title": None, "album": None},
        ])
    finally:
        await client.aclose()

    assert out[0].confidence == "none"
    assert out[0].usable is False


def test_suggestion_usable_property():
    """Pure unit: usability rules."""
    high = Suggestion(file_id=1, artist="A", title="T", album=None,
                      confidence="high", reasoning="")
    none = Suggestion(file_id=2, artist=None, title=None, album=None,
                      confidence="none", reasoning="")
    no_title = Suggestion(file_id=3, artist="A", title=None, album=None,
                          confidence="medium", reasoning="")
    none_with_text = Suggestion(file_id=4, artist="A", title="T", album=None,
                                confidence="none", reasoning="hallucination guard")
    assert high.usable
    assert not none.usable
    assert not no_title.usable
    assert not none_with_text.usable  # confidence='none' overrides text


@respx.mock
async def test_ai_scan_endpoint_happy_path(tmp_path, monkeypatch):
    """End-to-end: seed unmatched files, hit /api/ai_scan, verify DB updated."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    db = tmp_path / ".local2spoti" / "state.db"
    db.parent.mkdir(parents=True)
    async with connect(db) as conn:
        await init_schema(conn)
        now = datetime(2026, 5, 4, tzinfo=UTC)
        for i in range(2):
            await repo.upsert_local_file(conn, LocalFile(
                path=f"/m/track{i}.mp3", mtime=1, size=1, format="mp3",
                status=FileStatus.UNMATCHED,
            ), now=now)

    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=_claude_response([
            {"id": 1, "artist": "Beatles", "title": "Hey Jude",
             "album": None, "confidence": "high", "reasoning": "..."},
            {"id": 2, "artist": None, "title": None, "album": None,
             "confidence": "none", "reasoning": "..."},
        ]))
    )

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/ai_scan")

    assert r.status_code == 200
    data = r.json()
    assert data["processed"] == 2
    assert data["updated"] == 1  # only the high-confidence one
    assert data["by_confidence"]["high"] == 1
    assert data["by_confidence"]["none"] == 1

    # Verify DB write: file 1 should be back to 'scanned' with AI metadata
    async with connect(db) as conn:
        cur = await conn.execute(
            "SELECT artist, title, status, metadata_source FROM local_file WHERE id=1"
        )
        artist, title, status, source = await cur.fetchone()
    assert artist == "Beatles"
    assert title == "Hey Jude"
    assert status == "scanned"
    assert source == "ai"


async def test_ai_scan_rejects_missing_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/ai_scan")
    assert r.status_code == 400
    assert "ANTHROPIC_API_KEY" in r.json()["error"]
