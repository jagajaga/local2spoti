from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_settings
from .db import connect, init_schema
from .routes.ui import router as ui_router
from .state import AppState

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
    state = AppState(settings=settings)

    async with connect(settings.db_path) as conn:
        await init_schema(conn)
        state.db_conn = conn
        app.state.app_state = state
        yield


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
