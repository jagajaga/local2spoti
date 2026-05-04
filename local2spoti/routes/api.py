from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from .. import repo
from ..models import FileStatus

router = APIRouter(prefix="/api")


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
