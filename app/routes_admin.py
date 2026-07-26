"""Admin API + session auth for the web UI.

Login: POST /admin/api/login with the HUB_ADMIN_PASSWORD value → signed,
expiring session cookie (HMAC over an expiry timestamp with a secret persisted
in the DB). Everything under /admin/api/* except login requires the cookie.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets as pysecrets
import time

from fastapi import APIRouter, Cookie, HTTPException, Response
from pydantic import BaseModel, Field

from . import db, telegram
from .config import VERSION, settings
from .routes_bridge import perform_send
from .runtime import runtime

router = APIRouter(prefix="/admin/api")

SESSION_COOKIE = "hub_session"
SESSION_TTL = 7 * 24 * 3600
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _sign(expiry: int) -> str:
    mac = hmac.new(db.session_secret().encode(), str(expiry).encode(), hashlib.sha256)
    return f"{expiry}.{mac.hexdigest()}"


def _check_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expiry_raw, _, _mac = token.partition(".")
    try:
        expiry = int(expiry_raw)
    except ValueError:
        return False
    return expiry > time.time() and pysecrets.compare_digest(token, _sign(expiry))


def require_admin(hub_session: str | None = Cookie(default=None)) -> None:
    if not _check_session(hub_session):
        raise HTTPException(status_code=401, detail="admin login required")


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
async def login(req: LoginRequest, response: Response) -> dict:
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="HUB_ADMIN_PASSWORD is not set — admin UI is locked")
    if not pysecrets.compare_digest(req.password, settings.admin_password):
        await asyncio.sleep(1)  # slow down brute force
        raise HTTPException(status_code=401, detail="wrong password")
    token = _sign(int(time.time()) + SESSION_TTL)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax", path="/"
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/status")
async def status(hub_session: str | None = Cookie(default=None)) -> dict:
    """Unauthenticated probe: tells the UI whether a login session is active."""
    return {
        "version": VERSION,
        "authenticated": _check_session(hub_session),
        "deliveryMode": settings.resolved_delivery_mode(),
        "publicBaseUrl": settings.public_base_url or None,
        "adminPasswordSet": bool(settings.admin_password),
    }


def _lane_view(lane: dict) -> dict:
    mode = settings.resolved_delivery_mode()
    return {
        "slug": lane["slug"],
        "title": lane["title"],
        "botUsername": lane["bot_username"],
        "apiKey": lane["api_key"],
        "defaultChatId": lane["default_chat_id"],
        "enabled": bool(lane["enabled"]),
        "createdAt": lane["created_at"],
        "deliveryMode": mode,
        "webhookUrl": runtime.webhook_url(lane["slug"]) if mode == "webhook" else None,
        "polling": runtime.polling(lane["slug"]),
        "storedMessages": db.count_messages(lane["slug"]),
        "seenChats": db.seen_chats(lane["slug"]),
    }


class LaneCreate(BaseModel):
    slug: str
    title: str = ""
    bot_token: str = Field(alias="botToken")
    default_chat_id: str = Field(default="", alias="defaultChatId")

    model_config = {"populate_by_name": True}


class LaneUpdate(BaseModel):
    title: str | None = None
    bot_token: str | None = Field(default=None, alias="botToken")
    default_chat_id: str | None = Field(default=None, alias="defaultChatId")
    enabled: bool | None = None

    model_config = {"populate_by_name": True}


class AdminSend(BaseModel):
    text: str = Field(min_length=1, max_length=64000)
    chat_id: str | None = Field(default=None, alias="chatId")

    model_config = {"populate_by_name": True}


@router.get("/lanes")
async def lanes_list(hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    return {"lanes": [_lane_view(lane) for lane in db.list_lanes()]}


@router.post("/lanes", status_code=201)
async def lanes_create(req: LaneCreate, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    slug = req.slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(status_code=422, detail="slug must match [a-z0-9][a-z0-9_-]{0,31}")
    if slug in db.RESERVED_SLUGS:
        raise HTTPException(status_code=422, detail=f"slug '{slug}' is reserved")
    if db.get_lane(slug):
        raise HTTPException(status_code=409, detail="lane already exists")
    token = req.bot_token.strip()
    try:
        me = await telegram.get_me(token)
    except telegram.TelegramError as exc:
        raise HTTPException(status_code=422, detail=f"bot token rejected by Telegram: {exc}")
    lane_dict = db.create_lane(
        slug=slug,
        title=req.title.strip() or me.get("first_name", ""),
        bot_token=token,
        bot_username=me.get("username", ""),
        default_chat_id=req.default_chat_id.strip(),
    )
    warning = await runtime.sync_lane(lane_dict)
    view = _lane_view(lane_dict)
    if warning:
        view["warning"] = warning
    return view


@router.patch("/lanes/{slug}")
async def lanes_update(slug: str, req: LaneUpdate, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    lane = db.get_lane(slug)
    if not lane:
        raise HTTPException(status_code=404, detail="unknown lane")
    fields: dict = {}
    if req.title is not None:
        fields["title"] = req.title.strip()
    if req.default_chat_id is not None:
        fields["default_chat_id"] = req.default_chat_id.strip()
    if req.enabled is not None:
        fields["enabled"] = int(req.enabled)
    if req.bot_token is not None:
        token = req.bot_token.strip()
        try:
            me = await telegram.get_me(token)
        except telegram.TelegramError as exc:
            raise HTTPException(status_code=422, detail=f"bot token rejected by Telegram: {exc}")
        fields["bot_token"] = token
        fields["bot_username"] = me.get("username", "")
    lane = db.update_lane(slug, fields)
    assert lane is not None
    warning = await runtime.sync_lane(lane)
    view = _lane_view(lane)
    if warning:
        view["warning"] = warning
    return view


@router.post("/lanes/{slug}/rotate-key")
async def lanes_rotate_key(slug: str, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    if not db.get_lane(slug):
        raise HTTPException(status_code=404, detail="unknown lane")
    return {"apiKey": db.rotate_lane_key(slug)}


@router.delete("/lanes/{slug}")
async def lanes_delete(slug: str, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    lane = db.get_lane(slug)
    if not lane:
        raise HTTPException(status_code=404, detail="unknown lane")
    await runtime.remove_lane(lane)
    db.delete_lane(slug)
    return {"ok": True, "note": "lane removed; message history kept"}


@router.get("/lanes/{slug}/webhook-info")
async def lanes_webhook_info(slug: str, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    lane = db.get_lane(slug)
    if not lane:
        raise HTTPException(status_code=404, detail="unknown lane")
    try:
        return await telegram.get_webhook_info(lane["bot_token"])
    except telegram.TelegramError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/lanes/{slug}/send")
async def lanes_send(slug: str, req: AdminSend, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    lane = db.get_lane(slug)
    if not lane:
        raise HTTPException(status_code=404, detail="unknown lane")
    return await perform_send(lane, req.text, req.chat_id)


@router.get("/feed")
async def admin_feed(
    limit: int = 100,
    order: str = "desc",
    sinceDate: int = 0,
    chatId: int | None = None,
    hub_session: str | None = Cookie(default=None),
) -> dict:
    require_admin(hub_session)
    limit = max(1, min(limit, 500))
    if order not in ("asc", "desc"):
        order = "desc"
    rows = db.query_feed(sinceDate, limit, order, chatId)
    return {"messages": rows, "count": len(rows), "chats": db.seen_chats()}
