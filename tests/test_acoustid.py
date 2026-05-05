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


@respx.mock
async def test_lookup_raises_on_invalid_api_key():
    """Regression: AcoustID returns 200 with status=error when the API key
    is rejected. The client must raise instead of silently swallowing."""
    from local2spoti.acoustid import AcoustidError
    respx.get("https://api.acoustid.org/v2/lookup").mock(
        return_value=httpx.Response(200, json={
            "status": "error",
            "error": {"code": 4, "message": "invalid API key"},
        })
    )
    client = AcoustidClient(api_key="bogus")
    with pytest.raises(AcoustidError) as exc:
        await client.lookup(fingerprint="FP", duration=423)
    assert exc.value.code == 4
    assert "invalid API key" in exc.value.message


async def test_fingerprint_timeout_on_hung_subprocess(tmp_path, monkeypatch):
    """fpcalc on a corrupt file or unresponsive disk can hang forever;
    fingerprint() must bound it via timeout and return None."""
    if shutil.which("fpcalc") is None:
        pytest.skip("fpcalc required")
    # Stub `fpcalc` for this test with a tiny shell that just sleeps,
    # injected via PATH so create_subprocess_exec finds it first.
    fake_fpcalc = tmp_path / "fpcalc"
    fake_fpcalc.write_text("#!/bin/sh\nsleep 60\n")
    fake_fpcalc.chmod(0o755)
    import os
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    from local2spoti.acoustid import fingerprint
    import time
    t0 = time.monotonic()
    result = await fingerprint(tmp_path / "anything.mp3", timeout=0.3)
    elapsed = time.monotonic() - t0
    assert result is None
    assert elapsed < 2.0, f"timeout didn't fire — took {elapsed:.1f}s"
