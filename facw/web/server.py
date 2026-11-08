"""FastAPI application: routes, security headers, CORS, static console.

CORS is restricted to explicitly configured origins instead of ``*``
(spec 9.3 / pitfall 22).  By default only same-origin traffic is allowed; extra
origins can be added with ``FACW_ALLOWED_ORIGINS`` (comma separated).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.app import App
from ..core.logging_setup import get_logger
from . import api_more, api_routes, video

log = get_logger("web")


def _allowed_origins(port: int) -> list[str]:
    raw = os.environ.get("FACW_ALLOWED_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins:
        # same-host origins only (the console is served by this same server)
        origins = [
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
        ]
    return origins


def create_app(app_state: App) -> FastAPI:
    cfg = app_state.settings.get()
    api = FastAPI(
        title="FACW - Face Access Control Watch",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    api.state.app = app_state

    api.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(cfg.port),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    @api.middleware("http")
    async def security_headers(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace
            log.error("request failed", extra={"path": request.url.path, "error": str(exc)})
            return JSONResponse({"detail": "internal error"}, status_code=500)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elapsed = (time.perf_counter() - started) * 1000
        if elapsed > 1000:
            log.info("slow request", extra={"path": request.url.path, "ms": round(elapsed)})
        return response

    # ---- start-up / shut-down: start the pipeline with the server -----
    @api.on_event("startup")
    def _startup() -> None:
        app_state.start()

    @api.on_event("shutdown")
    def _shutdown() -> None:
        app_state.stop()

    # api_more must be registered first: it owns the literal "/api/events/stream"
    # route, which would otherwise be captured by api_routes' "/api/events/{id}"
    # parameterised route (FastAPI matches in registration order).
    api.include_router(api_more.router)
    api.include_router(api_routes.router)
    api.include_router(video.router)

    console_dir: Path = app_state.paths.console_dir
    assets_dir = console_dir / "assets"
    if assets_dir.exists():
        api.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @api.get("/")
    def index() -> FileResponse:
        return FileResponse(console_dir / "index.html")

    @api.get("/{path:path}")
    def spa(path: str):
        """Serve console files; unknown paths fall back to the shell.

        Guards against path traversal and keeps API 404s real.
        """
        if path.startswith("api/") or path.startswith("video/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = (console_dir / path).resolve()
        try:
            candidate.relative_to(console_dir.resolve())
        except ValueError:
            return JSONResponse({"detail": "not found"}, status_code=404)
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(console_dir / "index.html")

    return api


def bootstrap_admin(app_state: App) -> None:
    """Create the first administrator on a fresh install."""
    if app_state.users.count() > 0:
        return
    login = os.environ.get("FACW_ADMIN_USER", "admin")
    password = os.environ.get("FACW_ADMIN_PASSWORD", "admin12345")
    app_state.users.create(
        {
            "display_name": "Administrator",
            "login_name": login,
            "password": password,
            "role": "admin",
            "email": "admin@example.local",
        }
    )
    app_state.users.create(
        {
            "display_name": "Observer",
            "login_name": "observer",
            "password": os.environ.get("FACW_OBSERVER_PASSWORD", "observer12345"),
            "role": "observer",
            "email": "observer@example.local",
        }
    )
    log.warning(
        "created default accounts - change these passwords immediately",
        extra={"admin": login},
    )
