from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import repo

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
            "threshold": state.settings.threshold,
        },
    )
