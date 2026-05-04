import httpx
import respx
import pytest

from local2spoti.spotify_oauth import build_authorize_url, exchange_code, refresh_token, PKCE


def test_build_authorize_url():
    pkce = PKCE.generate()
    url = build_authorize_url(
        client_id="cid", redirect_uri="http://127.0.0.1:8000/callback",
        scope="x y", state="st", pkce=pkce,
    )
    assert "client_id=cid" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "scope=x+y" in url


@respx.mock
async def test_exchange_code():
    respx.post("https://accounts.spotify.com/api/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "atk", "refresh_token": "rtk",
            "expires_in": 3600, "scope": "x y", "token_type": "Bearer",
        })
    )
    pkce = PKCE.generate()
    out = await exchange_code(
        code="code123", client_id="cid",
        redirect_uri="http://127.0.0.1:8000/callback", pkce=pkce,
    )
    assert out["access_token"] == "atk"
    assert out["refresh_token"] == "rtk"


@respx.mock
async def test_refresh_token():
    respx.post("https://accounts.spotify.com/api/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "new", "expires_in": 3600,
            "scope": "x", "token_type": "Bearer",
        })
    )
    out = await refresh_token(refresh="rtk", client_id="cid")
    assert out["access_token"] == "new"


def test_pkce_verifier_length():
    p = PKCE.generate()
    assert 43 <= len(p.verifier) <= 128
