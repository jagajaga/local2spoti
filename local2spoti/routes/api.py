from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from .. import repo
from ..matcher import Threshold
from ..models import FileStatus
from ..pipeline import run_scan
from ..spotify_client import SpotifyClient

router = APIRouter(prefix="/api")
auth_router = APIRouter()


@router.post("/review/approve")
async def approve(request: Request, file_id: int = Form(...), track_id: str = Form(...)) -> JSONResponse:
    state = request.app.state.app_state
    await repo.update_match(state.db_conn, file_id,
                             spotify_track_id=track_id, confidence=1.0, method="manual")
    return JSONResponse({"ok": True})


@router.post("/review/skip")
async def skip(request: Request, file_id: int = Form(...)) -> JSONResponse:
    state = request.app.state.app_state
    await repo.set_status(state.db_conn, file_id, FileStatus.UNMATCHED)
    return JSONResponse({"ok": True})


@router.post("/review/approve_top_visible")
async def approve_top_visible(request: Request) -> JSONResponse:
    """Bulk-approve: takes file_ids from form (multiple values allowed),
    sets each to its top-ranked candidate."""
    form = await request.form()
    raw_ids = form.getlist("file_ids")
    ids: list[int] = []
    for v in raw_ids:
        ids.extend(int(x) for x in v.split(",") if x.strip())
    state = request.app.state.app_state
    if not ids:
        return JSONResponse({"approved": 0})
    placeholders = ",".join("?" * len(ids))
    cur = await state.db_conn.execute(
        f"""SELECT mc.local_file_id, mc.spotify_track_id, mc.confidence
            FROM match_candidate mc
            WHERE mc.rank = 1 AND mc.local_file_id IN ({placeholders})""",
        ids,
    )
    rows = await cur.fetchall()
    for fid, track_id, conf in rows:
        await repo.update_match(state.db_conn, fid,
                                 spotify_track_id=track_id, confidence=conf, method="manual")
    return JSONResponse({"approved": len(rows)})


@router.post("/threshold")
async def set_threshold(request: Request, threshold: str = Form(...)) -> JSONResponse:
    if threshold not in ("strict", "balanced", "loose"):
        return JSONResponse({"error": "invalid"}, status_code=400)
    state = request.app.state.app_state
    state.settings.threshold = threshold
    await state.db_conn.execute(
        "INSERT OR REPLACE INTO setting (key, value) VALUES ('threshold', ?)",
        (threshold,),
    )
    await state.db_conn.commit()
    return JSONResponse({"ok": True, "threshold": threshold})


@router.post("/scan/start")
async def scan_start(request: Request) -> JSONResponse:
    state = request.app.state.app_state
    if state.scan_task and not state.scan_task.done():
        return JSONResponse({"error": "scan already running"}, status_code=409)
    if not state.settings.library_root:
        return JSONResponse({"error": "library_root not configured"}, status_code=400)

    cur = await state.db_conn.execute(
        "SELECT access_token FROM auth_token WHERE key='spotify'"
    )
    row = await cur.fetchone()
    if not row:
        return JSONResponse({"error": "Spotify not connected"}, status_code=400)
    access_token = row[0]

    threshold = Threshold(state.settings.threshold)
    client = SpotifyClient(access_token=access_token, bucket=state.spotify_bucket)
    state.cancel_event.clear()

    async def _run() -> None:
        try:
            await run_scan(
                conn=state.db_conn, client=client,
                library_root=Path(state.settings.library_root),
                threshold=threshold, bus=state.bus,
            )
        finally:
            await client.aclose()

    state.scan_task = asyncio.create_task(_run())
    return JSONResponse({"ok": True})


@router.post("/scan/cancel")
async def scan_cancel(request: Request) -> JSONResponse:
    state = request.app.state.app_state
    if state.scan_task and not state.scan_task.done():
        state.cancel_event.set()
        state.scan_task.cancel()
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "no scan running"}, status_code=400)


import secrets
from datetime import UTC, datetime, timedelta

from fastapi.responses import RedirectResponse

from ..spotify_oauth import (
    DEFAULT_SCOPE, PKCE, build_authorize_url, exchange_code,
)

# Module-level pkce store keyed by state token (one user, in-memory)
_PKCE_STORE: dict[str, PKCE] = {}


@auth_router.get("/auth/login")
async def auth_login(request: Request) -> RedirectResponse:
    state = request.app.state.app_state
    if not state.settings.spotify_client_id:
        return JSONResponse(
            {"error": "spotify_client_id not configured. Set LOCAL2SPOTI_SPOTIFY_CLIENT_ID."},
            status_code=400,
        )
    pkce = PKCE.generate()
    state_token = secrets.token_urlsafe(16)
    _PKCE_STORE[state_token] = pkce
    redirect_uri = f"http://127.0.0.1:{state.settings.port}/callback"
    url = build_authorize_url(
        client_id=state.settings.spotify_client_id,
        redirect_uri=redirect_uri,
        scope=DEFAULT_SCOPE,
        state=state_token,
        pkce=pkce,
    )
    return RedirectResponse(url, status_code=307)


@auth_router.get("/callback")
async def auth_callback(request: Request) -> RedirectResponse:
    code = request.query_params.get("code")
    state_token = request.query_params.get("state")
    if not code or not state_token or state_token not in _PKCE_STORE:
        return JSONResponse({"error": "invalid callback"}, status_code=400)
    pkce = _PKCE_STORE.pop(state_token)
    state = request.app.state.app_state
    redirect_uri = f"http://127.0.0.1:{state.settings.port}/callback"
    tokens = await exchange_code(
        code=code,
        client_id=state.settings.spotify_client_id,
        redirect_uri=redirect_uri,
        pkce=pkce,
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=tokens["expires_in"] - 60)
    from ..spotify_client import SpotifyClient
    client = SpotifyClient(access_token=tokens["access_token"], bucket=state.spotify_bucket)
    try:
        me = await client.me()
    finally:
        await client.aclose()
    await state.db_conn.execute(
        """INSERT OR REPLACE INTO auth_token (key, access_token, refresh_token,
                                              expires_at, scope, user_id)
           VALUES ('spotify', ?, ?, ?, ?, ?)""",
        (tokens["access_token"], tokens["refresh_token"],
         expires_at.isoformat(), tokens["scope"], me["id"]),
    )
    await state.db_conn.commit()
    return RedirectResponse("/dashboard", status_code=307)
