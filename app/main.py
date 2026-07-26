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
from .routes_bridge import router as bridge_router
from .routes_portal import router as portal_router
from .runtime import runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

STATIC_DIR = Path(__file__).parent / "static"


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
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/admin")

    @app.get("/admin", include_in_schema=False)
    async def admin_ui() -> FileResponse:
        return FileResponse(STATIC_DIR / "admin.html")

    @app.get("/portal", include_in_schema=False)
    async def portal_ui() -> FileResponse:
        return FileResponse(STATIC_DIR / "portal.html")

    app.include_router(admin_router)
    app.include_router(portal_router)
    app.include_router(bridge_router)
    return app


app = create_app()
