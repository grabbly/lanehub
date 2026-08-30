"""Member self-service API (the member half of the unified web UI).

A member is invited by the admin (email + generated password), logs in through
the single sign-in form, creates their OWN bot lane by pasting a BotFather
token, and can come back any time for their API key, agent recipes, or a
password change. Members can only ever touch their own lane.

Login/logout are handled by the unified `/api/*` endpoints (routes_auth); these
endpoints authenticate via the shared `hub_session` cookie.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel, Field

from . import db, telegram
from .config import settings
from .routes_admin import budget_caps, derive_slug, token_subject
from .runtime import runtime

router = APIRouter(prefix="/portal/api")


def require_member(hub_session: str | None) -> dict:
    subject = token_subject(hub_session)
    if not subject or subject == "admin":
        raise HTTPException(status_code=401, detail="member login required")
    member = db.get_member(subject)
    if not member:
        raise HTTPException(status_code=401, detail="account no longer exists")
    return member


class PortalLaneCreate(BaseModel):
    bot_token: str = Field(alias="botToken")
    title: str = ""

    model_config = {"populate_by_name": True}


class PasswordChange(BaseModel):
    old_password: str = Field(alias="oldPassword")
    new_password: str = Field(alias="newPassword", min_length=8)

    model_config = {"populate_by_name": True}


def _lane_view(member: dict) -> dict | None:
    lane = db.get_lane(member["lane_slug"]) if member["lane_slug"] else None
    if not lane:
        return None
    mode = settings.resolved_delivery_mode()
    return {
        "slug": lane["slug"],
        "title": lane["title"],
        "botUsername": lane["bot_username"],
        "apiKey": lane["api_key"],
        "defaultChatId": lane["default_chat_id"],
        "enabled": bool(lane["enabled"]),
        "deliveryMode": mode,
        "replyMode": db.get_lane_state(lane["slug"], "reply_mode") or "auto",
        "operatorChatId": db.get_lane_state(lane["slug"], "operator_chat_id") or "",
        "storedMessages": db.count_messages(lane["slug"]),
        "seenChats": db.seen_chats(lane["slug"]),
        "baseUrl": f"{settings.public_base_url}/{lane['slug']}" if settings.public_base_url else f"/{lane['slug']}",
    }


@router.get("/me")
async def me(hub_session: str | None = Cookie(default=None)) -> dict:
    member = require_member(hub_session)
    return {
        "email": member["email"],
        "name": member["name"],
        "lane": _lane_view(member),
        "projectChatId": db.get_hub_state("project_chat_id") or None,
    }


@router.get("/logs")
async def logs(hub_session: str | None = Cookie(default=None)) -> dict:
    """Watcher-activity log for the member's OWN lane only."""
    member = require_member(hub_session)
    slug = member["lane_slug"]
    if not slug or not db.get_lane(slug):
        return {"logs": []}
    ctx = int(db.get_lane_state(slug, "last_ctx_tokens") or 0)
    return {"slug": slug, "logs": db.query_lane_logs(slug), "ctxTokens": ctx}


class OperatorConfig(BaseModel):
    operator_chat_id: str | None = Field(default=None, alias="operatorChatId")
    reply_mode: str | None = Field(default=None, alias="replyMode")

    model_config = {"populate_by_name": True}


@router.post("/lane/operator")
async def set_operator(req: OperatorConfig, hub_session: str | None = Cookie(default=None)) -> dict:
    """Member sets THEIR OWN lane's operator chat + reply mode (nobody else's)."""
    member = require_member(hub_session)
    slug = member["lane_slug"]
    if not slug or not db.get_lane(slug):
        raise HTTPException(status_code=404, detail="no lane yet")
    if req.operator_chat_id is not None:
        db.set_lane_state(slug, "operator_chat_id", req.operator_chat_id.strip())
    if req.reply_mode is not None:
        db.set_lane_state(slug, "reply_mode", "confirm" if req.reply_mode == "confirm" else "auto")
    return {"lane": _lane_view(member)}


@router.post("/lane", status_code=201)
async def create_lane(req: PortalLaneCreate, hub_session: str | None = Cookie(default=None)) -> dict:
    member = require_member(hub_session)
    if member["lane_slug"] and db.get_lane(member["lane_slug"]):
        raise HTTPException(status_code=409, detail="you already have a lane")
    token = req.bot_token.strip()
    try:
        bot = await telegram.get_me(token)
    except telegram.TelegramError as exc:
        raise HTTPException(status_code=422, detail=f"bot token rejected by Telegram: {exc}")
    slug = derive_slug(bot.get("username", ""))
    lane = db.create_lane(
        slug=slug,
        title=req.title.strip() or member["name"] or bot.get("first_name", ""),
        bot_token=token,
        bot_username=bot.get("username", ""),
        default_chat_id=db.get_hub_state("project_chat_id") or "",
    )
    db.update_member(member["email"], {"lane_slug": slug})
    warning = await runtime.sync_lane(lane)
    member = db.get_member(member["email"]) or member
    view = _lane_view(member)
    assert view is not None
    if warning:
        view["warning"] = warning
    return view


@router.post("/lane/rotate-key")
async def rotate_key(hub_session: str | None = Cookie(default=None)) -> dict:
    member = require_member(hub_session)
    if not member["lane_slug"] or not db.get_lane(member["lane_slug"]):
        raise HTTPException(status_code=404, detail="you have no lane yet")
    return {"apiKey": db.rotate_lane_key(member["lane_slug"])}


@router.post("/password")
async def change_password(req: PasswordChange, hub_session: str | None = Cookie(default=None)) -> dict:
    member = require_member(hub_session)
    if not db.check_password(req.old_password, member["password_hash"]):
        await asyncio.sleep(1)
        raise HTTPException(status_code=401, detail="current password is wrong")
    db.update_member(member["email"], {"password_hash": db.hash_password(req.new_password)})
    return {"ok": True}
