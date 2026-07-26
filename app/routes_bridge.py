"""Per-lane public API: what agents and scripts call.

    POST /{lane}/send      — send a message as this lane's bot
    GET  /{lane}/messages  — this lane's history (humans + own sends)
    GET  /{lane}/feed      — the WHOLE chat merged across all lanes
    GET  /{lane}/info      — lane diagnostics
    POST /{lane}/webhook   — Telegram push receiver (secret-token auth)

Auth: X-Bridge-Token header must equal the lane's API key (except /webhook,
which Telegram authenticates with X-Telegram-Bot-Api-Secret-Token).
"""
from __future__ import annotations

import secrets as pysecrets

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import db, telegram
from .config import settings
from .runtime import ingest_update, runtime

router = APIRouter()


def _lane_or_404(lane_slug: str) -> dict:
    lane = db.get_lane(lane_slug)
    if not lane:
        raise HTTPException(status_code=404, detail="unknown lane")
    return lane


def _auth_lane(lane_slug: str, token: str) -> dict:
    lane = _lane_or_404(lane_slug)
    if not token or not pysecrets.compare_digest(token, lane["api_key"]):
        raise HTTPException(status_code=401, detail="invalid bridge token")
    if not lane["enabled"]:
        raise HTTPException(status_code=403, detail="lane is disabled")
    return lane


class SendRequest(BaseModel):
    text: str = Field(min_length=1, max_length=64000)
    chat_id: str | None = Field(default=None, alias="chatId")

    model_config = {"populate_by_name": True}


async def perform_send(lane: dict, text: str, chat_id: str | None) -> dict:
    """Send text as the lane's bot (splitting long text into <=4000-char parts)
    and record each part as a synthetic outgoing row so other lanes' readers
    see it in /feed (Telegram never delivers a bot's messages to other bots)."""
    # fallback chain: explicit chatId → lane default → hub-wide project chat
    target = (chat_id or lane["default_chat_id"] or db.get_hub_state("project_chat_id") or "").strip()
    if not target:
        raise HTTPException(
            status_code=503,
            detail="no chat_id configured; pass chatId in the body or set the lane's default chat",
        )
    parts = telegram.chunk_text(text)
    first_result: dict | None = None
    for part in parts:
        try:
            result = await telegram.send_message(lane["bot_token"], target, part)
        except telegram.TelegramError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        if first_result is None:
            first_result = result
        chat = result.get("chat", {})
        sender = result.get("from", {})
        db.store_message(
            lane_slug=lane["slug"],
            update_id=db.next_outgoing_update_id(lane["slug"]),
            message_id=result.get("message_id"),
            chat_id=chat.get("id"),
            chat_title=chat.get("title") or chat.get("username"),
            from_user=sender.get("username") or lane["bot_username"] or lane["slug"],
            text=result.get("text", part),
            date=result.get("date", 0),
            is_outgoing=True,
        )
    assert first_result is not None
    return {
        "ok": True,
        "messageId": first_result.get("message_id"),
        "chatId": first_result.get("chat", {}).get("id"),
        "parts": len(parts),
    }


@router.post("/{lane_slug}/send")
async def send(lane_slug: str, req: SendRequest, x_bridge_token: str = Header(default="")) -> dict:
    lane = _auth_lane(lane_slug, x_bridge_token)
    return await perform_send(lane, req.text, req.chat_id)


@router.get("/{lane_slug}/messages")
async def messages(
    lane_slug: str,
    since: int = Query(default=0, description="return messages with updateId > since"),
    limit: int = Query(default=50, ge=1, le=500),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    x_bridge_token: str = Header(default=""),
) -> dict:
    _auth_lane(lane_slug, x_bridge_token)
    rows = db.query_messages(lane_slug, since, limit, order)
    return {"messages": rows, "count": len(rows)}


@router.get("/{lane_slug}/feed")
async def feed(
    lane_slug: str,
    limit: int = Query(default=50, ge=1, le=500),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    since_date: int = Query(default=0, alias="sinceDate", description="unix seconds; date > sinceDate"),
    chat_id: int | None = Query(default=None, alias="chatId"),
    x_bridge_token: str = Header(default=""),
) -> dict:
    _auth_lane(lane_slug, x_bridge_token)
    rows = db.query_feed(since_date, limit, order, chat_id)
    return {"messages": rows, "count": len(rows)}


@router.get("/{lane_slug}/info")
async def info(lane_slug: str, x_bridge_token: str = Header(default="")) -> dict:
    lane = _auth_lane(lane_slug, x_bridge_token)
    mode = settings.resolved_delivery_mode()
    return {
        "lane": lane["slug"],
        "botUsername": lane["bot_username"],
        "defaultChatId": lane["default_chat_id"] or None,
        "deliveryMode": mode,
        "webhookUrl": runtime.webhook_url(lane["slug"]) if mode == "webhook" else None,
        "polling": runtime.polling(lane["slug"]),
        "storedMessages": db.count_messages(lane["slug"]),
        "seenChats": db.seen_chats(lane["slug"]),
    }


@router.post("/{lane_slug}/webhook")
async def webhook(
    lane_slug: str,
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict:
    """Telegram webhook receiver, authenticated by the per-lane secret token
    Telegram echoes back in the X-Telegram-Bot-Api-Secret-Token header."""
    lane = _lane_or_404(lane_slug)
    if not pysecrets.compare_digest(x_telegram_bot_api_secret_token, lane["webhook_secret"]):
        raise HTTPException(status_code=403, detail="bad secret token")
    update = await request.json()
    if isinstance(update, dict) and "update_id" in update:
        ingest_update(lane_slug, update)
    return {"ok": True}
