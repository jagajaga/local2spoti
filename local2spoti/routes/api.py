from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import repo
from ..acoustid import AcoustidClient, AcoustidError, fingerprint, fpcalc_available
from ..ai_match import AIClient
from ..events import ProgressEvent
from ..matcher import Threshold
from ..models import FileStatus
from ..pipeline import run_scan
from ..playlist import push_matched_to_spotify
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


@router.post("/review/approve_above_confidence")
async def approve_above_confidence(
    request: Request, threshold: float = Form(...),
) -> JSONResponse:
    """Bulk-approve every review-queue file whose top candidate's confidence
    is at or above `threshold` (0.0–1.0). Hits the entire queue, not just
    the visible page.
    """
    if not 0.0 <= threshold <= 1.0:
        return JSONResponse(
            {"error": "threshold must be between 0.0 and 1.0"}, status_code=400,
        )
    state = request.app.state.app_state
    cur = await state.db_conn.execute(
        """SELECT mc.local_file_id, mc.spotify_track_id, mc.confidence
           FROM match_candidate mc
           JOIN local_file lf ON lf.id = mc.local_file_id
           WHERE mc.rank = 1 AND lf.status = 'review' AND mc.confidence >= ?""",
        (threshold,),
    )
    rows = await cur.fetchall()
    for fid, track_id, conf in rows:
        await repo.update_match(
            state.db_conn, fid, spotify_track_id=track_id,
            confidence=conf, method="manual",
        )
    return JSONResponse(
        {"approved": len(rows), "threshold": threshold,
         "message": f"Approved {len(rows)} files with confidence ≥ {int(threshold * 100)}%"}
    )


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


@router.post("/reset")
async def reset(request: Request) -> JSONResponse:
    """Wipe all scan state (files, candidates, playlists, scan runs).

    Preserves:
      - auth_token (so you don't have to re-OAuth)
      - setting (library_root, threshold, etc.)
    """
    state = request.app.state.app_state
    if state.scan_task and not state.scan_task.done():
        return JSONResponse(
            {"error": "scan running — stop it first"}, status_code=409,
        )
    conn = state.db_conn
    # Order matters: children before parents (FKs cascade but be explicit)
    await conn.execute("DELETE FROM playlist_track")
    await conn.execute("DELETE FROM playlist")
    await conn.execute("DELETE FROM match_candidate")
    await conn.execute("DELETE FROM local_file")
    await conn.execute("DELETE FROM scan_run")
    await conn.commit()
    return JSONResponse({"ok": True})


@router.post("/library")
async def set_library(request: Request, path: str = Form(...)) -> JSONResponse:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        return JSONResponse({"error": "not a directory"}, status_code=400)
    state = request.app.state.app_state
    state.settings.library_root = p
    await state.db_conn.execute(
        "INSERT OR REPLACE INTO setting (key, value) VALUES ('library_root', ?)",
        (str(p),),
    )
    await state.db_conn.commit()
    return JSONResponse({"library_root": str(p)})


@router.get("/browse", response_class=HTMLResponse)
async def browse(request: Request, path: str | None = None) -> HTMLResponse:
    """Render an HTMX fragment listing subdirectories of `path`.

    Defaults to the user's home dir. Hidden entries (starting with '.') are
    skipped. Returns HTML so HTMX can swap it directly into the page.
    """
    base = Path(path).expanduser() if path else Path.home()
    try:
        base = base.resolve()
    except OSError:
        return HTMLResponse(f'<div class="text-red-400 text-sm">invalid path: {path}</div>')
    if not base.is_dir():
        return HTMLResponse(
            f'<div class="text-red-400 text-sm">not a directory: {base}</div>'
        )

    entries: list[dict] = []
    try:
        for entry in os.scandir(base):
            if entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                entries.append({"name": entry.name, "path": str(base / entry.name)})
    except PermissionError:
        return HTMLResponse(
            f'<div class="text-red-400 text-sm">permission denied: {base}</div>'
        )
    entries.sort(key=lambda e: e["name"].lower())

    parent = str(base.parent) if base.parent != base else None
    rows = []
    if parent is not None:
        rows.append(
            f'<button type="button" class="w-full text-left px-2 py-1 hover:bg-zinc-800 rounded text-sm" '
            f'hx-get="/api/browse?path={parent}" hx-target="#browser-body" hx-swap="innerHTML">'
            f'<span class="text-zinc-500">↑</span> ..</button>'
        )
    for e in entries:
        rows.append(
            f'<button type="button" class="w-full text-left px-2 py-1 hover:bg-zinc-800 rounded text-sm truncate" '
            f'hx-get="/api/browse?path={e["path"]}" hx-target="#browser-body" hx-swap="innerHTML">'
            f'<span class="text-amber-400">📁</span> {e["name"]}</button>'
        )
    if not entries:
        rows.append('<div class="text-zinc-500 text-xs px-2 py-1">(no subfolders)</div>')

    body = "".join(rows)
    return HTMLResponse(
        f'<div class="text-xs text-zinc-400 mb-2 break-all">'
        f'<span class="font-mono">{base}</span></div>'
        f'<div class="max-h-64 overflow-y-auto bg-zinc-950 rounded p-1">{body}</div>'
        f'<input type="hidden" id="browser-current-path" value="{base}">'
    )


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


@router.post("/push")
async def push(request: Request) -> JSONResponse:
    state = request.app.state.app_state
    cur = await state.db_conn.execute(
        "SELECT access_token FROM auth_token WHERE key='spotify'"
    )
    row = await cur.fetchone()
    if not row:
        return JSONResponse({"error": "Spotify not connected"}, status_code=400)
    client = SpotifyClient(access_token=row[0], bucket=state.spotify_bucket)
    try:
        result = await push_matched_to_spotify(conn=state.db_conn, client=client)
    finally:
        await client.aclose()
    return JSONResponse({"playlists_created": result.playlists_created, "added": result.added})


@router.post("/deep_scan")
async def deep_scan(request: Request, limit: int = 200) -> JSONResponse:
    """Kick off an AcoustID deep scan as a background task.

    Returns immediately so the UI doesn't block; progress events stream over
    the WebSocket and surface in the dashboard's progress bar.
    """
    state = request.app.state.app_state
    if not fpcalc_available():
        return JSONResponse({"error": "fpcalc not installed"}, status_code=400)
    if not state.settings.acoustid_api_key:
        return JSONResponse({"error": "acoustid_api_key not set"}, status_code=400)
    if state.scan_task and not state.scan_task.done():
        return JSONResponse(
            {"error": "another job is running — stop it first"}, status_code=409,
        )

    cur = await state.db_conn.execute(
        "SELECT id, path, duration_ms FROM local_file WHERE status='unmatched' LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    if not rows:
        return JSONResponse(
            {"ok": True, "updated": 0, "message": "No unmatched files to deep-scan"}
        )
    total = len(rows)

    async def _run() -> None:
        acoustid = AcoustidClient(api_key=state.settings.acoustid_api_key)
        # Per-file outcomes — without this breakdown we couldn't tell
        # "0 matched" from "API key invalid" from "fpcalc segfaulted".
        outcomes = {"matched": 0, "no_match": 0, "fpcalc_failed": 0, "api_error": 0}
        processed = 0
        await state.bus.publish(ProgressEvent(
            stage="deep_scan", processed=0, total=total,
            message=f"fingerprinting {total} files",
        ))
        try:
            for fid, path_str, _dur_ms in rows:
                if state.cancel_event.is_set():
                    await state.bus.publish(ProgressEvent(
                        stage="deep_scan", processed=processed, total=total,
                        message="cancelled",
                    ))
                    return
                fp = await fingerprint(Path(path_str))
                if fp is None:
                    outcomes["fpcalc_failed"] += 1
                else:
                    dur, fingerprint_str = fp
                    try:
                        md = await acoustid.lookup(
                            fingerprint=fingerprint_str, duration=dur,
                        )
                    except AcoustidError as err:
                        # Bail loudly on auth/quota errors — every other file
                        # would just hit the same wall.
                        await state.bus.publish(ProgressEvent(
                            stage="deep_scan", processed=processed, total=total,
                            message=f"AcoustID error {err.code}: {err.message} — aborting",
                        ))
                        return
                    if md is None:
                        outcomes["no_match"] += 1
                    else:
                        await state.db_conn.execute(
                            """UPDATE local_file SET artist=?, title=?, status='scanned',
                               metadata_source='acoustid' WHERE id=?""",
                            (md.artist, md.title, fid),
                        )
                        await state.db_conn.commit()
                        outcomes["matched"] += 1
                processed += 1
                await state.bus.publish(ProgressEvent(
                    stage="deep_scan", processed=processed, total=total,
                    message=f"matched {outcomes['matched']} / "
                            f"no_match {outcomes['no_match']} / "
                            f"fpcalc_failed {outcomes['fpcalc_failed']}",
                ))
            await state.bus.publish(ProgressEvent(
                stage="deep_scan", processed=total, total=total,
                message=(
                    f"done — matched {outcomes['matched']}, "
                    f"no_match {outcomes['no_match']}, "
                    f"fpcalc_failed {outcomes['fpcalc_failed']}"
                ),
            ))
        finally:
            await acoustid.aclose()
            await state.bus.flush()

    state.cancel_event.clear()
    state.scan_task = asyncio.create_task(_run())
    return JSONResponse(
        {
            "ok": True,
            "message": f"Deep scan started — {total} files queued, watch the progress bar",
        }
    )


@router.post("/ai_scan")
async def ai_scan(request: Request, batch_size: int = 20, limit: int = 100) -> JSONResponse:
    """Kick off Claude metadata identification as a background task.

    Returns immediately. Progress events stream over the WebSocket and surface
    in the dashboard's progress bar. Final summary is included as the
    `message` of the last event when finished.
    """
    state = request.app.state.app_state
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return JSONResponse(
            {"error": "ANTHROPIC_API_KEY not set"}, status_code=400,
        )
    if state.scan_task and not state.scan_task.done():
        return JSONResponse(
            {"error": "another job is running — stop it first"}, status_code=409,
        )

    cur = await state.db_conn.execute(
        """SELECT id, path, artist, title, album FROM local_file
           WHERE status='unmatched' LIMIT ?""",
        (limit,),
    )
    rows = await cur.fetchall()
    if not rows:
        return JSONResponse(
            {"ok": True, "message": "No unmatched files for AI scan"}
        )

    files = [
        {"id": r[0], "path": r[1], "artist": r[2], "title": r[3], "album": r[4]}
        for r in rows
    ]
    total = len(files)

    async def _run() -> None:
        ai = AIClient()  # reads ANTHROPIC_API_KEY + CLAUDE_MODEL from env
        by_confidence: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "none": 0}
        updated = 0
        processed = 0
        await state.bus.publish(ProgressEvent(
            stage="ai_scan", processed=0, total=total,
            message=f"sending {total} files to Claude in batches of {batch_size}",
        ))
        try:
            for i in range(0, total, batch_size):
                if state.cancel_event.is_set():
                    await state.bus.publish(ProgressEvent(
                        stage="ai_scan", processed=processed, total=total,
                        message="cancelled",
                    ))
                    return
                batch = files[i : i + batch_size]
                try:
                    suggestions = await ai.suggest_metadata(batch)
                except Exception as e:
                    await state.bus.publish(ProgressEvent(
                        stage="ai_scan", processed=processed, total=total,
                        message=f"failed: {e}",
                    ))
                    return
                for s in suggestions:
                    by_confidence[s.confidence] = by_confidence.get(s.confidence, 0) + 1
                    if s.usable:
                        await state.db_conn.execute(
                            """UPDATE local_file SET artist=?, title=?, album=?,
                               status='scanned', metadata_source='ai' WHERE id=?""",
                            (s.artist, s.title, s.album, s.file_id),
                        )
                        updated += 1
                await state.db_conn.commit()
                processed += len(batch)
                await state.bus.publish(ProgressEvent(
                    stage="ai_scan", processed=processed, total=total,
                    message=f"updated {updated}, "
                            f"high {by_confidence['high']} / "
                            f"medium {by_confidence['medium']} / "
                            f"low {by_confidence['low']} / "
                            f"none {by_confidence['none']}",
                ))
            await state.bus.publish(ProgressEvent(
                stage="ai_scan", processed=total, total=total,
                message=f"done — {updated} files have AI metadata, "
                        "click Start scan to run Spotify match",
            ))
        finally:
            await ai.aclose()
            await state.bus.flush()

    state.cancel_event.clear()
    state.scan_task = asyncio.create_task(_run())
    return JSONResponse(
        {
            "ok": True,
            "message": f"AI scan started — {total} files queued, watch the progress bar",
        }
    )


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
