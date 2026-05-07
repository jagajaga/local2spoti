from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import repo
from ..models import FileStatus

router = APIRouter()


def _templates() -> Jinja2Templates:
    tmpl_dir = Path(__file__).parent.parent / "templates"
    return Jinja2Templates(directory=str(tmpl_dir))


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    state = request.app.state.app_state
    counts = await repo.count_by_status(state.db_conn)
    cur = await state.db_conn.execute("SELECT COUNT(*) FROM local_file")
    (total_files,) = await cur.fetchone()
    cur = await state.db_conn.execute(
        "SELECT MAX(finished_at) FROM scan_run WHERE status='completed'"
    )
    (last_scan_at,) = await cur.fetchone()
    # Per-method match breakdown so the user can see at a glance how
    # many tracks landed via fingerprint paths (musicbrainz / odesli /
    # isrc) vs the regular Spotify search match.
    cur = await state.db_conn.execute(
        "SELECT match_method, COUNT(*) FROM local_file "
        "WHERE spotify_track_id IS NOT NULL GROUP BY match_method"
    )
    by_method = {row[0] or "search": row[1] for row in await cur.fetchall()}
    cur = await state.db_conn.execute(
        "SELECT COUNT(*) FROM local_file WHERE isrc IS NOT NULL"
    )
    (isrc_tagged,) = await cur.fetchone()
    user_row = await (await state.db_conn.execute(
        "SELECT user_id FROM auth_token WHERE key='spotify'"
    )).fetchone()
    return _templates().TemplateResponse(
        request,
        "dashboard.html",
        {
            "library_root": str(state.settings.library_root) if state.settings.library_root else None,
            "total_files": total_files,
            "last_scan_at": last_scan_at,
            "spotify_user": user_row[0] if user_row else None,
            "counts": {k.value: v for k, v in counts.items()},
            "by_method": by_method,
            "isrc_tagged": isrc_tagged,
            "threshold": state.settings.threshold,
            "has_acoustid_key": bool(state.settings.acoustid_api_key),
        },
    )


@router.get("/files")
async def files(
    request: Request,
    status: str = Query("matched"),
    limit: int = 100000,  # effectively unbounded for typical libraries
):
    # Review and unmatched have richer dedicated pages — keep one canonical
    # URL per status so the table view and the candidate-card view never
    # diverge.
    if status == "review":
        return RedirectResponse("/review", status_code=307)
    if status == "unmatched":
        return RedirectResponse("/unmatched", status_code=307)

    state = request.app.state.app_state
    try:
        st = FileStatus(status)
    except ValueError:
        st = FileStatus.MATCHED
    files = await repo.list_files_by_status(state.db_conn, st, limit=limit, offset=0)
    return _templates().TemplateResponse(
        request,
        "files.html",
        {
            "files": files,
            "status": status,
            "statuses": [s.value for s in FileStatus],
        },
    )


@router.get("/review", response_class=HTMLResponse)
async def review(request: Request, limit: int = 100000) -> HTMLResponse:
    state = request.app.state.app_state
    files = await repo.list_files_by_status(state.db_conn, FileStatus.REVIEW,
                                             limit=limit, offset=0)
    cards = []
    for f in files:
        cur = await state.db_conn.execute(
            """SELECT spotify_track_id, spotify_artist, spotify_title, spotify_album,
                      confidence, artist_similarity, title_similarity, rank
               FROM match_candidate WHERE local_file_id=? ORDER BY rank LIMIT 5""",
            (f.id,),
        )
        candidates = [
            {"spotify_track_id": r[0], "artist": r[1], "title": r[2], "album": r[3],
             "confidence": r[4], "artist_sim": r[5], "title_sim": r[6], "rank": r[7]}
            for r in await cur.fetchall()
        ]
        cards.append({"file": f, "candidates": candidates})
    return _templates().TemplateResponse(
        request, "review.html", {"cards": cards},
    )


@router.get("/scan", response_class=HTMLResponse)
async def scan(request: Request) -> HTMLResponse:
    return _templates().TemplateResponse(request, "scan.html", {})


@router.get("/unmatched", response_class=HTMLResponse)
async def unmatched(request: Request, limit: int = 100000) -> HTMLResponse:
    state = request.app.state.app_state
    files = await repo.list_files_by_status(state.db_conn, FileStatus.UNMATCHED,
                                             limit=limit, offset=0)
    return _templates().TemplateResponse(
        request, "files.html",
        {"files": files, "status": "unmatched",
         "statuses": [s.value for s in FileStatus]},
    )
