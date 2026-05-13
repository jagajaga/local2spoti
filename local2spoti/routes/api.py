from __future__ import annotations

import asyncio
import os
from datetime import UTC
from datetime import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import repo
from ..acoustid import fpcalc_available
from ..matcher import Threshold
from ..models import FileStatus
from ..pipeline import _stage_match, run_scan
from ..playlist import push_matched_to_spotify
from ..recovery import (
    ai_scan_unmatched,
    auto_cycle,
    count_unmatched,
    deep_scan_unmatched,
    match_via_mb_text,
)
from ..spotify_client import SpotifyClient


def _make_token_provider(state):
    """Returns an async callable the SpotifyClient invokes on 401.

    Calling it actively refreshes the token via the OAuth refresh
    endpoint (instead of just re-reading the DB), so a 401 race against
    the periodic refresh_loop still recovers cleanly. Without this, a
    file that 401s the moment its access token crosses the 1-hour
    boundary would error out before refresh_loop's next 60-second tick
    has run.
    """
    from ..token_refresh import refresh_if_expiring

    async def _provide() -> str:
        try:
            await refresh_if_expiring(
                conn=state.db_conn,
                client_id=state.settings.spotify_client_id,
                threshold_seconds=86400,  # always refresh on demand
            )
        except Exception:
            pass
        cur = await state.db_conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
        row = await cur.fetchone()
        return row[0] if row else ""

    return _provide


router = APIRouter(prefix="/api")
auth_router = APIRouter()


@router.post("/review/approve")
async def approve(request: Request, file_id: int = Form(...), track_id: str = Form(...)) -> JSONResponse:
    state = request.app.state.app_state
    await repo.update_match(state.db_conn, file_id, spotify_track_id=track_id, confidence=1.0, method="manual")
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
        await repo.update_match(state.db_conn, fid, spotify_track_id=track_id, confidence=conf, method="manual")
    return JSONResponse({"approved": len(rows)})


@router.post("/review/approve_above_confidence")
async def approve_above_confidence(
    request: Request,
    threshold: float = Form(...),
) -> JSONResponse:
    """Bulk-approve every review-queue file whose top candidate's confidence
    is at or above `threshold` (0.0–1.0). Hits the entire queue, not just
    the visible page.
    """
    if not 0.0 <= threshold <= 1.0:
        return JSONResponse(
            {"error": "threshold must be between 0.0 and 1.0"},
            status_code=400,
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
            state.db_conn,
            fid,
            spotify_track_id=track_id,
            confidence=conf,
            method="manual",
        )
    return JSONResponse(
        {
            "approved": len(rows),
            "threshold": threshold,
            "message": f"Approved {len(rows)} files with confidence ≥ {int(threshold * 100)}%",
        }
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
    if state.any_job_running():
        return JSONResponse(
            {"error": "a scan/deep_scan/ai_scan is running — stop it first"},
            status_code=409,
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
        return HTMLResponse(f'<div class="text-red-400 text-sm">not a directory: {base}</div>')

    entries: list[dict] = []
    try:
        for entry in os.scandir(base):
            if entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                entries.append({"name": entry.name, "path": str(base / entry.name)})
    except PermissionError:
        return HTMLResponse(f'<div class="text-red-400 text-sm">permission denied: {base}</div>')
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
        return JSONResponse(
            {"error": "Spotify scan already running — stop it first"},
            status_code=409,
        )
    if not state.settings.library_root:
        return JSONResponse({"error": "library_root not configured"}, status_code=400)

    cur = await state.db_conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
    row = await cur.fetchone()
    if not row:
        return JSONResponse({"error": "Spotify not connected"}, status_code=400)
    access_token = row[0]

    threshold = Threshold(state.settings.threshold)
    client = SpotifyClient(
        access_token=access_token,
        bucket=state.spotify_bucket,
        token_provider=_make_token_provider(state),
    )
    # Only clear the cancel event if no other long-running job is using it.
    if not state.any_job_running():
        state.cancel_event.clear()

    async def _run() -> None:
        """Smart Start scan — full pipeline + automatic recovery passes.

        Phases (each emits its own bar, all share the same scan_task slot):
          1. Standard scan (discovery + metadata + match)
          2. If unmatched files remain AND AcoustID configured →
             deep_scan_unmatched, then re-run the match stage on the newly
             promoted 'scanned' files.
          3. If unmatched files STILL remain AND ANTHROPIC_API_KEY is set →
             ai_scan_unmatched, then re-run match.

        Each phase stops on cancel_event. If neither key is set, phases
        2-3 are skipped and you just get the standard scan.
        """
        try:
            await run_scan(
                conn=state.db_conn,
                client=client,
                library_root=Path(state.settings.library_root),
                threshold=threshold,
                bus=state.bus,
            )
            if state.cancel_event.is_set():
                return

            # Phase 2: AcoustID rescue
            if fpcalc_available() and state.settings.acoustid_api_key and await count_unmatched(state.db_conn) > 0:
                await deep_scan_unmatched(state)
                if state.cancel_event.is_set():
                    return
                await _stage_match(
                    state.db_conn,
                    client,
                    threshold,
                    bus=state.bus,
                    now=_dt.now(UTC),
                )
                if state.cancel_event.is_set():
                    return

            # Phase 3: AI rescue
            if os.environ.get("ANTHROPIC_API_KEY") and await count_unmatched(state.db_conn) > 0:
                await ai_scan_unmatched(state)
                if state.cancel_event.is_set():
                    return
                await _stage_match(
                    state.db_conn,
                    client,
                    threshold,
                    bus=state.bus,
                    now=_dt.now(UTC),
                )
        finally:
            await client.aclose()

    state.scan_task = asyncio.create_task(_run())
    return JSONResponse(
        {
            "ok": True,
            "message": "Smart scan started — will auto-run AcoustID + AI on whatever stays unmatched",
        }
    )


@router.post("/clear_rate_limit_pause")
async def clear_rate_limit_pause(request: Request) -> JSONResponse:
    """Drop any active Spotify rate-limit pause on the token bucket.

    Use when the bucket is parked from a stale 429 (e.g. an earlier run
    received a multi-hour Retry-After) and you want to probe Spotify now
    rather than wait it out. If they're still throttled, the next call
    will just 429 again and pause for a fresh (capped at 5min) interval.
    """
    state = request.app.state.app_state
    remaining = state.spotify_bucket.pause_remaining()
    state.spotify_bucket.clear_pause()
    return JSONResponse(
        {
            "ok": True,
            "message": f"Cleared rate-limit pause (was {remaining:.0f}s remaining)",
        }
    )


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Drop the stored Spotify OAuth tokens.

    User can re-OAuth via /auth/login afterwards. We refuse if a job is
    running so we don't yank the token out from under an active match.
    """
    state = request.app.state.app_state
    if state.any_job_running():
        return JSONResponse(
            {"error": "stop running jobs first"},
            status_code=409,
        )
    cur = await state.db_conn.execute("DELETE FROM auth_token WHERE key='spotify'")
    await state.db_conn.commit()
    return JSONResponse(
        {"ok": True, "message": f"Disconnected from Spotify ({cur.rowcount} token cleared)"},
    )


@router.post("/retry_error/{file_id}")
async def retry_one_error(request: Request, file_id: int) -> JSONResponse:
    """Move a single file from status='error' back to 'scanned' and drop
    any stale candidates. Used by the per-row retry button on the
    /files?status=error view."""
    state = request.app.state.app_state
    cur = await state.db_conn.execute(
        "SELECT status FROM local_file WHERE id=?",
        (file_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return JSONResponse({"error": "file not found"}, status_code=404)
    if row[0] != "error":
        return JSONResponse(
            {"error": f"file is not in error status (currently {row[0]})"},
            status_code=400,
        )
    await state.db_conn.execute(
        "DELETE FROM match_candidate WHERE local_file_id=?",
        (file_id,),
    )
    await state.db_conn.execute(
        "UPDATE local_file SET status='scanned', last_error=NULL WHERE id=?",
        (file_id,),
    )
    await state.db_conn.commit()
    return JSONResponse({"ok": True, "message": "Reset to scanned — click Match to retry"})


@router.post("/skip_by_artist")
async def skip_by_artist(request: Request, artist: str = Form(...)) -> JSONResponse:
    """Mark every file whose artist tag is exactly `artist` as
    status='skipped'. Clears any existing Spotify match + candidates
    so the file won't be in the push pool either.

    Idempotent. Reversible via /api/unskip_by_artist.
    """
    state = request.app.state.app_state
    cur = await state.db_conn.execute("SELECT id FROM local_file WHERE artist=?", (artist,))
    ids = [r[0] for r in await cur.fetchall()]
    if not ids:
        return JSONResponse({"ok": True, "skipped": 0, "message": f"no files with artist={artist!r}"})
    placeholders = ",".join("?" * len(ids))
    await state.db_conn.execute(
        f"DELETE FROM match_candidate WHERE local_file_id IN ({placeholders})", ids
    )
    await state.db_conn.execute(
        f"""UPDATE local_file SET
            status='skipped', spotify_track_id=NULL,
            match_confidence=NULL, match_method=NULL,
            last_error='skipped: artist={artist}'
            WHERE id IN ({placeholders})""",
        ids,
    )
    await state.db_conn.commit()
    return JSONResponse(
        {
            "ok": True,
            "skipped": len(ids),
            "message": f"Skipped {len(ids)} files with artist={artist!r}",
        }
    )


@router.post("/unskip_by_artist")
async def unskip_by_artist(request: Request, artist: str = Form(...)) -> JSONResponse:
    """Reverse `/api/skip_by_artist`: move every skipped file with this
    artist back to 'unmatched' so future Match/fingerprint/MB-text
    runs will reconsider them.
    """
    state = request.app.state.app_state
    cur = await state.db_conn.execute(
        "UPDATE local_file SET status='unmatched', last_error=NULL "
        "WHERE artist=? AND status='skipped'",
        (artist,),
    )
    await state.db_conn.commit()
    n = cur.rowcount
    return JSONResponse(
        {"ok": True, "unskipped": n, "message": f"Unskipped {n} files with artist={artist!r}"}
    )


@router.post("/reevaluate_review")
async def reevaluate_review(request: Request) -> JSONResponse:
    """Re-score every review-queue file's stored candidates against
    the current matcher rules (variant penalty + tighter duration
    window), without hitting Spotify.

    For each review file:
      1. Recompute variant_mismatch from the stored spotify_title vs
         the file's title.
      2. Apply the variant penalty (-0.30 confidence).
      3. Re-rank candidates by new confidence.
      4. Run decide() on the new top. If "auto", promote the file to
         status='matched'. If "unmatched" (variant guard pushed it
         down hard), demote to status='unmatched'. Otherwise leave it
         in review.

    Returns the per-outcome counts. Zero Spotify API calls — purely a
    DB pass.
    """
    from ..matcher import Threshold, _has_variant_marker, decide

    state = request.app.state.app_state
    threshold = Threshold(state.settings.threshold)
    cur = await state.db_conn.execute(
        """SELECT lf.id, lf.artist, lf.title,
                  mc.id, mc.spotify_track_id, mc.spotify_title,
                  mc.artist_similarity, mc.title_similarity,
                  mc.duration_delta_ms, mc.confidence, mc.spotify_album
           FROM local_file lf
           JOIN match_candidate mc ON mc.local_file_id = lf.id
           WHERE lf.status='review'
           ORDER BY lf.id, mc.confidence DESC"""
    )
    rows = await cur.fetchall()
    by_file: dict[int, list[tuple]] = {}
    local_titles: dict[int, str] = {}
    for r in rows:
        fid = r[0]
        local_titles[fid] = r[2] or ""
        by_file.setdefault(fid, []).append(r)

    outcomes = {"promoted": 0, "demoted": 0, "still_review": 0}
    for fid, cands in by_file.items():
        local_title = local_titles[fid]
        local_has_variant = _has_variant_marker(local_title)
        # Re-score each candidate against the new variant rule. We only
        # apply the variant penalty here — we don't recompute artist/
        # title similarity because those are deterministic from the
        # already-stored fields.
        rescored = []
        for c in cands:
            (
                _fid,
                _artist,
                _title,
                _cand_id,
                track_id,
                sp_title,
                a_sim,
                t_sim,
                dur_delta,
                old_conf,
                sp_album,
            ) = c
            variant_mismatch = bool(sp_title and _has_variant_marker(sp_title)) and not local_has_variant
            penalty = 0.30 if variant_mismatch else 0.0
            # Reconstruct confidence as it would be today. The +0.05
            # album bonus / +0.10 dur bonus are already baked into
            # old_conf, so just subtract the new penalty.
            new_conf = max(0.0, old_conf - penalty)
            rescored.append((new_conf, a_sim, t_sim, dur_delta, sp_album, variant_mismatch, track_id))
        rescored.sort(reverse=True)
        new_top = rescored[0]
        new_conf, a_sim, t_sim, dur_delta, sp_album, vm, track_id = new_top
        decision = decide(
            artist_sim=a_sim,
            title_sim=t_sim,
            album_match=False,  # conservative — the BALANCED rule no longer uses album anyway
            duration_delta_ms=dur_delta,
            threshold=threshold,
            variant_mismatch=vm,
        )
        if decision == "auto":
            await state.db_conn.execute(
                """UPDATE local_file SET
                    spotify_track_id=?, match_confidence=?,
                    match_method='auto', status='matched'
                   WHERE id=?""",
                (track_id, new_conf, fid),
            )
            outcomes["promoted"] += 1
        elif decision == "unmatched":
            await state.db_conn.execute(
                "UPDATE local_file SET status='unmatched' WHERE id=?",
                (fid,),
            )
            outcomes["demoted"] += 1
        else:
            outcomes["still_review"] += 1
    await state.db_conn.commit()
    return JSONResponse(
        {
            "ok": True,
            "message": (
                f"re-evaluated {len(by_file)} review files — "
                f"{outcomes['promoted']} promoted to matched, "
                f"{outcomes['demoted']} demoted to unmatched, "
                f"{outcomes['still_review']} still in review"
            ),
            **outcomes,
        }
    )


@router.post("/retry_errors")
async def retry_errors(request: Request) -> JSONResponse:
    """Move every status='error' file back to 'scanned' so the next match
    can retry them. Clears last_error and any stale match_candidate rows.

    Most common reason files end up in 'error': Spotify's /search endpoint
    soft-blocks our IP after a burst of requests and 403s for a while.
    Files marked as failed during the block can be retried once Spotify
    lifts it (usually a few hours later, with the slower rate limiter).
    """
    state = request.app.state.app_state
    if state.any_job_running():
        return JSONResponse(
            {"error": "another job is running — stop it first"},
            status_code=409,
        )
    cur = await state.db_conn.execute("SELECT id FROM local_file WHERE status='error'")
    ids = [r[0] for r in await cur.fetchall()]
    if not ids:
        return JSONResponse({"ok": True, "retried": 0, "message": "no files in error status"})
    # Clear stale candidates and reset to scanned in a single transaction.
    placeholders = ",".join("?" * len(ids))
    await state.db_conn.execute(
        f"DELETE FROM match_candidate WHERE local_file_id IN ({placeholders})",
        ids,
    )
    await state.db_conn.execute(
        f"""UPDATE local_file SET status='scanned', last_error=NULL
            WHERE id IN ({placeholders})""",
        ids,
    )
    await state.db_conn.commit()
    return JSONResponse(
        {
            "ok": True,
            "retried": len(ids),
            "message": f"Reset {len(ids)} error files → scanned. Click Match to retry.",
        }
    )


@router.post("/match_via_mb_text")
async def match_via_mb_text_endpoint(request: Request, limit: int = 100000) -> JSONResponse:
    """Alternative to /api/match: uses MusicBrainz text search on the
    file's existing artist/title/album tags to land a Spotify track ID
    via MB's URL relationships (or ISRC, or Odesli) — no Spotify
    /search calls.

    Use this when Spotify's /search is rate-limited (403'd) and you
    want to keep matching. MB has its own 1 req/sec bucket, separate
    from Spotify's, so this stage progresses regardless of Spotify's
    backoff state. Coverage is lower than Spotify's fuzzy /search
    (MB text-search misses on heavily-decorated titles), so this is
    additive — files MB can't find stay in 'scanned'.

    Counts as the deep_scan job slot (it runs alongside the Spotify
    bucket, but doesn't compete with /api/match's bucket use).
    """
    state = request.app.state.app_state
    if state.deep_scan_task and not state.deep_scan_task.done():
        return JSONResponse(
            {"error": "Deep scan / fingerprint / mb-text match already running — stop it first"},
            status_code=409,
        )
    if not state.any_job_running():
        state.cancel_event.clear()
    state.deep_scan_task = asyncio.create_task(
        match_via_mb_text(state, limit=limit),
    )
    return JSONResponse(
        {
            "ok": True,
            "message": "MB-text match started — bypasses Spotify /search via MB recording search",
        }
    )


@router.post("/match_via_fingerprint")
async def match_via_fingerprint(request: Request, limit: int = 100000) -> JSONResponse:
    """Alternative to /api/match: uses AcoustID fingerprinting + MusicBrainz
    URL relationships to map files directly to Spotify track IDs without
    ever calling /v1/search.

    Operates on every file still needing a Spotify match
    (status='scanned' OR 'unmatched', spotify_track_id IS NULL). Files
    that resolve via MB go to status='matched' immediately. Files where
    MB has no Spotify URL fall back to AcoustID-tagged status='scanned'
    (so a subsequent /api/match run can finish them). Files that AcoustID
    can't identify stay where they were.

    Why use this: dramatically reduces Spotify rate-limit pressure by
    skipping /v1/search for every track that's MusicBrainz-registered.
    Free (uses only AcoustID + MusicBrainz APIs, both free tiers).
    """
    state = request.app.state.app_state
    if not fpcalc_available():
        return JSONResponse({"error": "fpcalc not installed"}, status_code=400)
    if not state.settings.acoustid_api_key:
        return JSONResponse({"error": "acoustid_api_key not set"}, status_code=400)
    if state.deep_scan_task and not state.deep_scan_task.done():
        return JSONResponse(
            {"error": "Deep scan / fingerprint match already running — stop it first"},
            status_code=409,
        )
    if not state.any_job_running():
        state.cancel_event.clear()
    state.deep_scan_task = asyncio.create_task(
        deep_scan_unmatched(state, limit=limit, statuses=("scanned", "unmatched")),
    )
    return JSONResponse(
        {
            "ok": True,
            "message": "Fingerprint match started — bypasses Spotify /search via MusicBrainz lookups",
        }
    )


@router.post("/match")
async def match_only(request: Request) -> JSONResponse:
    """Run JUST the Spotify match stage on every status='scanned' file.

    Use this when you have a backlog of scanned files (e.g. AI/AcoustID
    promoted them, or a previous scan was cancelled mid-match) and you
    want to match them without re-walking the filesystem or kicking off
    a full smart pipeline. Counts as the 'scan' job slot since it uses
    the Spotify rate-limit bucket.
    """
    state = request.app.state.app_state
    if state.scan_task and not state.scan_task.done():
        return JSONResponse(
            {"error": "Spotify match/scan already running — stop it first"},
            status_code=409,
        )
    cur = await state.db_conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
    row = await cur.fetchone()
    if not row:
        return JSONResponse({"error": "Spotify not connected"}, status_code=400)

    threshold = Threshold(state.settings.threshold)
    client = SpotifyClient(access_token=row[0], bucket=state.spotify_bucket, token_provider=_make_token_provider(state))
    if not state.any_job_running():
        state.cancel_event.clear()

    async def _run() -> None:
        try:
            await _stage_match(
                state.db_conn,
                client,
                threshold,
                bus=state.bus,
                now=_dt.now(UTC),
            )
        finally:
            await client.aclose()

    state.scan_task = asyncio.create_task(_run())
    return JSONResponse(
        {
            "ok": True,
            "message": "Match started — processing scanned files",
        }
    )


@router.post("/auto_cycle")
async def auto_cycle_endpoint(request: Request) -> JSONResponse:
    """Run match → AI(review) → AI(unmatched) on a loop until nothing
    moves. The "button 6" — one click chews through the long tail of
    review/unmatched files that need both Spotify search and Claude
    rescue, instead of you alternating manually.

    Stops when an iteration produces zero new matches, hits Stop, or
    hits the safety cap of 10 iterations.
    """
    state = request.app.state.app_state
    if state.auto_cycle_task and not state.auto_cycle_task.done():
        return JSONResponse(
            {"error": "Auto-cycle already running — stop it first"},
            status_code=409,
        )
    if state.scan_task and not state.scan_task.done():
        return JSONResponse(
            {"error": "Match/scan already running — stop it first (auto-cycle drives the same slot)"},
            status_code=409,
        )
    cur = await state.db_conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
    if not await cur.fetchone():
        return JSONResponse({"error": "Spotify not connected"}, status_code=400)
    if not state.any_job_running():
        state.cancel_event.clear()
    state.auto_cycle_task = asyncio.create_task(auto_cycle(state))
    return JSONResponse({"ok": True, "message": "Auto-cycle started — match + AI rescue, looping until stable"})


@router.post("/scan/cancel")
async def scan_cancel(request: Request) -> JSONResponse:
    """Stop every running long-running job (scan / deep_scan / ai_scan / auto_cycle)."""
    state = request.app.state.app_state
    cancelled: list[str] = []
    state.cancel_event.set()
    for label, task in (
        ("scan", state.scan_task),
        ("deep_scan", state.deep_scan_task),
        ("ai_scan", state.ai_scan_task),
        ("auto_cycle", state.auto_cycle_task),
    ):
        if task is not None and not task.done():
            task.cancel()
            cancelled.append(label)
    if not cancelled:
        return JSONResponse({"error": "no jobs running"}, status_code=400)
    return JSONResponse({"ok": True, "message": "Stopped: " + ", ".join(cancelled)})


@router.post("/push")
async def push(request: Request) -> JSONResponse:
    state = request.app.state.app_state
    cur = await state.db_conn.execute("SELECT access_token FROM auth_token WHERE key='spotify'")
    row = await cur.fetchone()
    if not row:
        return JSONResponse({"error": "Spotify not connected"}, status_code=400)
    client = SpotifyClient(access_token=row[0], bucket=state.spotify_bucket, token_provider=_make_token_provider(state))
    try:
        result = await push_matched_to_spotify(conn=state.db_conn, client=client)
    finally:
        await client.aclose()
    return JSONResponse({"playlists_created": result.playlists_created, "added": result.added})


@router.post("/deep_scan")
async def deep_scan(
    request: Request,
    limit: int = 100000,
    status: str = "unmatched",
) -> JSONResponse:
    """Kick off an AcoustID deep scan as a background task.

    `status` selects the source pool (defaults to 'unmatched'; the /review
    page passes 'review' to re-fingerprint files with bad candidates).
    """
    state = request.app.state.app_state
    if status not in ("unmatched", "review"):
        return JSONResponse({"error": f"invalid status: {status}"}, status_code=400)
    if not fpcalc_available():
        return JSONResponse({"error": "fpcalc not installed"}, status_code=400)
    if not state.settings.acoustid_api_key:
        return JSONResponse({"error": "acoustid_api_key not set"}, status_code=400)
    if state.deep_scan_task and not state.deep_scan_task.done():
        return JSONResponse(
            {"error": "Deep scan already running — stop it first"},
            status_code=409,
        )
    if not state.any_job_running():
        state.cancel_event.clear()
    state.deep_scan_task = asyncio.create_task(deep_scan_unmatched(state, limit=limit, statuses=(status,)))
    return JSONResponse(
        {
            "ok": True,
            "message": f"Deep scan started on {status} files — watch the bar",
        }
    )


@router.post("/ai_scan")
async def ai_scan(
    request: Request,
    batch_size: int = 20,
    limit: int = 100000,
    status: str = "unmatched",
) -> JSONResponse:
    """Kick off Claude metadata identification as a background task.

    Returns immediately. Progress events stream over the WebSocket and surface
    in the dashboard's progress bar. Final summary is included as the
    `message` of the last event when finished.
    """
    state = request.app.state.app_state
    if status not in ("unmatched", "review"):
        return JSONResponse({"error": f"invalid status: {status}"}, status_code=400)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return JSONResponse(
            {"error": "ANTHROPIC_API_KEY not set"},
            status_code=400,
        )
    if state.ai_scan_task and not state.ai_scan_task.done():
        return JSONResponse(
            {"error": "AI scan already running — stop it first"},
            status_code=409,
        )
    if not state.any_job_running():
        state.cancel_event.clear()
    state.ai_scan_task = asyncio.create_task(
        ai_scan_unmatched(state, batch_size=batch_size, limit=limit, status=status)
    )
    return JSONResponse(
        {
            "ok": True,
            "message": f"AI scan started on {status} files — watch the bar",
        }
    )


import secrets
from datetime import datetime, timedelta

from fastapi.responses import RedirectResponse

from ..spotify_oauth import (
    DEFAULT_SCOPE,
    PKCE,
    build_authorize_url,
    exchange_code,
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

    client = SpotifyClient(
        access_token=tokens["access_token"],
        bucket=state.spotify_bucket,
        token_provider=_make_token_provider(state),
    )
    try:
        me = await client.me()
    finally:
        await client.aclose()
    await state.db_conn.execute(
        """INSERT OR REPLACE INTO auth_token (key, access_token, refresh_token,
                                              expires_at, scope, user_id)
           VALUES ('spotify', ?, ?, ?, ?, ?)""",
        (
            tokens["access_token"],
            tokens["refresh_token"],
            expires_at.isoformat(),
            tokens["scope"],
            me["id"],
        ),
    )
    await state.db_conn.commit()
    return RedirectResponse("/dashboard", status_code=307)
