"""Login for the single-operator web UI.

One form, one cookie (`hub_session`), one role: the operator signs in with
`HUB_ADMIN_PASSWORD`. The token machinery (sign/verify, secret, TTL) is shared
with the rest of the app via routes_admin; this module only checks the password
and issues the cookie. Data endpoints live under /admin/api/* and authenticate
via the same `hub_session` cookie.
"""
from __future__ import annotations

import asyncio
import secrets as pysecrets
import time

from fastapi import APIRouter, Cookie, HTTPException, Response
from pydantic import BaseModel

from .config import VERSION, settings
from .routes_admin import SESSION_COOKIE, SESSION_TTL, sign_token, token_subject


def is_authenticated(token: str | None) -> bool:
    """Whether a session cookie carries the operator subject."""
    return token_subject(token) == "admin"


router = APIRouter(prefix="/api")


class LoginRequest(BaseModel):
    # `email` is accepted for backward-compatible request bodies but ignored —
    # the single operator signs in with the password only.
    email: str = ""
    password: str


@router.post("/login")
async def login(req: LoginRequest, response: Response) -> dict:
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="HUB_ADMIN_PASSWORD is not set — the hub is locked")
    if not pysecrets.compare_digest(req.password, settings.admin_password):
        await asyncio.sleep(1)  # slow down brute force
        raise HTTPException(status_code=401, detail="wrong password")
    token = sign_token("admin", int(time.time()) + SESSION_TTL)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax", path="/"
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/session")
async def session(hub_session: str | None = Cookie(default=None)) -> dict:
    """Unauthenticated probe: whether an operator session is active."""
    return {
        "version": VERSION,
        "authenticated": is_authenticated(hub_session),
        "deliveryMode": settings.resolved_delivery_mode(),
        "publicBaseUrl": settings.public_base_url or None,
        "adminPasswordSet": bool(settings.admin_password),
    }
