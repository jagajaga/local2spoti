import shutil
import pytest

from local2spoti.acoustid import fpcalc_available, AcoustidClient
import respx
import httpx


def test_fpcalc_detection():
    assert fpcalc_available() == (shutil.which("fpcalc") is not None)


@respx.mock
async def test_lookup_returns_top_match():
    respx.get("https://api.acoustid.org/v2/lookup").mock(
        return_value=httpx.Response(200, json={
            "status": "ok",
            "results": [{
                "id": "abc",
                "score": 0.99,
                "recordings": [{
                    "id": "rec1",
                    "title": "Around the World",
                    "artists": [{"name": "Daft Punk"}],
                }],
            }],
        })
    )
    client = AcoustidClient(api_key="test")
    md = await client.lookup(fingerprint="FP", duration=423)
    assert md is not None
    assert md.artist == "Daft Punk"
    assert md.title == "Around the World"


@respx.mock
async def test_lookup_no_match():
    respx.get("https://api.acoustid.org/v2/lookup").mock(
        return_value=httpx.Response(200, json={"status": "ok", "results": []})
    )
    client = AcoustidClient(api_key="test")
    md = await client.lookup(fingerprint="FP", duration=423)
    assert md is None
