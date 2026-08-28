"""Unified login for the single web UI.

One form, one cookie (`hub_session`), two roles:

- blank email + password  → superadmin (checked against HUB_ADMIN_PASSWORD)
- email + password        → member (checked against the members table)

The token machinery (sign/verify, secret, TTL) is shared with the rest of the
app via routes_admin; this module only decides the role and issues the cookie.
Data endpoints stay under /admin/api/* and /portal/api/* and authenticate via
the same `hub_session` cookie.
"""
from __future__ import annotations

import asyncio
import secrets as pysecrets
import time

from fastapi import APIRouter, Cookie, HTTPException, Response
from pydantic import BaseModel

from . import db
from .config import VERSION, settings
from .routes_admin import SESSION_COOKIE, SESSION_TTL, sign_token, token_subject

router = APIRouter(prefix="/api")


def resolve_role(token: str | None) -> str | None:
    """Role carried by a session cookie: 'admin', 'member', or None."""
    subject = token_subject(token)
    if subject == "admin":
        return "admin"
    if subject and db.get_member(subject):
        return "member"
    return None


class LoginRequest(BaseModel):
    email: str = ""
    password: str


@router.post("/login")
async def login(req: LoginRequest, response: Response) -> dict:
    email = req.email.strip().lower()
    if not email:
        # superadmin path — password only
        if not settings.admin_password:
            raise HTTPException(status_code=503, detail="HUB_ADMIN_PASSWORD is not set — admin is locked")
        if not pysecrets.compare_digest(req.password, settings.admin_password):
            await asyncio.sleep(1)  # slow down brute force
            raise HTTPException(status_code=401, detail="wrong password")
        subject, role = "admin", "admin"
    else:
        # member path — email + password
        member = db.get_member(email)
        if not member or not db.check_password(req.password, member["password_hash"]):
            await asyncio.sleep(1)
            raise HTTPException(status_code=401, detail="wrong email or password")
        db.update_member(member["email"], {"last_login": int(time.time())})
        subject, role = member["email"], "member"

    token = sign_token(subject, int(time.time()) + SESSION_TTL)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax", path="/"
    )
    return {"ok": True, "role": role}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/session")
async def session(hub_session: str | None = Cookie(default=None)) -> dict:
    """Unauthenticated probe: whether a session is active and which role it is."""
    role = resolve_role(hub_session)
    return {
        "version": VERSION,
        "authenticated": role is not None,
        "role": role,
        "deliveryMode": settings.resolved_delivery_mode(),
        "publicBaseUrl": settings.public_base_url or None,
        "adminPasswordSet": bool(settings.admin_password),
    }
