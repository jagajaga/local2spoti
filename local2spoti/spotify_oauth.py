from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

DEFAULT_SCOPE = (
    "playlist-modify-private playlist-modify-public "
    "playlist-read-private user-read-private"
)


@dataclass(frozen=True)
class PKCE:
    verifier: str
    challenge: str

    @classmethod
    def generate(cls) -> "PKCE":
        verifier = secrets.token_urlsafe(64)[:96]
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return cls(verifier=verifier, challenge=challenge)


def build_authorize_url(
    *, client_id: str, redirect_uri: str, scope: str, state: str, pkce: PKCE,
) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(
    *, code: str, client_id: str, redirect_uri: str, pkce: PKCE,
) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as h:
        r = await h.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": pkce.verifier,
            },
        )
        r.raise_for_status()
        return r.json()


async def refresh_token(*, refresh: str, client_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as h:
        r = await h.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            },
        )
        r.raise_for_status()
        return r.json()
