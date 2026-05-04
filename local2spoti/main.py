from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Load .env into os.environ before anything else reads it. This lets the
# Anthropic SDK auto-pick up ANTHROPIC_API_KEY without us threading it through
# Settings, and respects the user's existing dotenv conventions.
load_dotenv()

from .config import load_settings
from .db import connect, init_schema
from .logging_config import configure as configure_logging
from .routes.api import router as api_router, auth_router
from .routes.ui import router as ui_router
from .routes.ws import router as ws_router
from .state import AppState
from .token_refresh import refresh_loop

try:
    import uvloop
    _HAS_UVLOOP = True
except ImportError:
    _HAS_UVLOOP = False


def _templates_dir() -> Path:
    return Path(__file__).parent / "templates"


def _static_dir() -> Path:
    return Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.ensure_dirs()
    configure_logging(settings.log_dir)
    state = AppState(settings=settings)

    async with connect(settings.db_path) as conn:
        await init_schema(conn)
        # Restore persisted settings from the `setting` table
        cur = await conn.execute("SELECT key, value FROM setting")
        for k, v in await cur.fetchall():
            if k == "library_root":
                state.settings.library_root = Path(v)
            elif k == "threshold":
                state.settings.threshold = v  # type: ignore[assignment]
            elif k == "acoustid_api_key":
                state.settings.acoustid_api_key = v
        state.db_conn = conn
        app.state.app_state = state

        refresh_task = asyncio.create_task(
            refresh_loop(conn=conn, client_id=settings.spotify_client_id)
        )
        try:
            yield
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except (asyncio.CancelledError, Exception):
                pass


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    app = FastAPI(title="Local2Spoti", lifespan=lifespan)

    static = _static_dir()
    if static.exists():
        app.mount("/static", StaticFiles(directory=str(static)), name="static")

    @app.get("/")
    async def root():
        return RedirectResponse("/dashboard", status_code=307)

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok"})

    app.include_router(ui_router)
    app.include_router(api_router)
    app.include_router(auth_router)
    app.include_router(ws_router)

    return app


def run() -> None:
    import uvicorn
    if _HAS_UVLOOP:
        uvloop.install()
    settings = load_settings()
    uvicorn.run(
        "local2spoti.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
