"""LaneHub — self-hosted Telegram bridge hub.

App wiring: fixed routes (health/version/admin) are registered BEFORE the
dynamic /{lane}/... bridge router so they can never be shadowed by a lane
slug; reserved slugs are additionally rejected at lane creation.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

from . import db
from .config import VERSION, settings
from .routes_admin import router as admin_router
from .routes_auth import router as auth_router
from .routes_bridge import router as bridge_router
from .routes_portal import router as portal_router
from .runtime import runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

STATIC_DIR = Path(__file__).parent / "static"
WATCHER_FILE = Path(__file__).parent.parent / "scripts" / "telegram_watch.py"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect().close()  # create schema up front
    await runtime.sync_all()
    try:
        yield
    finally:
        await runtime.stop_all()


def create_app() -> FastAPI:
    app = FastAPI(title="LaneHub", version=VERSION, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/version")
    async def version() -> dict:
        return {"name": "LaneHub", "version": VERSION, "deliveryMode": settings.resolved_delivery_mode()}

    @app.get("/", include_in_schema=False)
    async def root() -> FileResponse:
        """The single web UI: one login, then role-based sections."""
        return FileResponse(STATIC_DIR / "app.html")

    # Old split entrances redirect to the unified one (invite emails, docs, bookmarks).
    @app.get("/admin", include_in_schema=False)
    async def admin_ui() -> RedirectResponse:
        return RedirectResponse(url="/", status_code=307)

    @app.get("/portal", include_in_schema=False)
    async def portal_ui() -> RedirectResponse:
        return RedirectResponse(url="/", status_code=307)

    @app.get("/watcher.py", include_in_schema=False)
    async def watcher_script() -> FileResponse:
        """The @mention watcher, served straight from the hub so onboarding can
        `curl {hub}/watcher.py` and always get the version matching this hub."""
        return FileResponse(WATCHER_FILE, media_type="text/x-python", filename="telegram_watch.py")

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(portal_router)
    app.include_router(bridge_router)
    return app


app = create_app()
