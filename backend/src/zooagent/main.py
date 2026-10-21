"""FastAPI application factory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import api_router
from .errors import ZooAgentError
from .state import AppState

DEV_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
]


def resolve_web_dir() -> Path | None:
    """Directory holding the built React app.

    Checked in order: explicit override, the directory bundled into the
    PyInstaller sidecar, then the monorepo layout (dev).
    """
    override = os.environ.get("ZOOAGENT_WEB_DIR")
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_dir() else None

    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidate = Path(bundle_dir) / "web"
        if candidate.is_dir():
            return candidate

    repo_root = Path(__file__).resolve().parents[3]
    candidate = repo_root / "apps" / "desktop" / "dist"
    return candidate if candidate.is_dir() else None


def create_app(state: AppState | None = None) -> FastAPI:
    state = state or AppState.create()
    app = FastAPI(
        title="zooagent",
        version=__version__,
        description="Local-first AI application platform backend",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.za = state

    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ZooAgentError)
    async def _domain_error(_: Request, exc: ZooAgentError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    app.include_router(api_router)

    web_dir = resolve_web_dir()
    if web_dir is not None:
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    else:

        @app.get("/")
        def root() -> dict[str, str]:
            return {
                "name": "zooagent",
                "version": __version__,
                "hint": "前端未构建。开发模式请运行 apps/desktop 的 vite dev server。",
                "api": "/api/health",
            }

    return app
