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
import time

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import db, operator, telegram
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


class WakeAck(BaseModel):
    wake_id: int = Field(alias="wakeId")
    session_id: str | None = Field(default=None, alias="sessionId")

    model_config = {"populate_by_name": True}


def _scan_mention(lane: dict, cursor: int) -> tuple[dict | None, list[dict]]:
    """Look past `cursor` for the first @mention of the lane's bot. Returns
    (mention_row | None, scanned_incoming_rows). Read-only — no state change."""
    bot_username = lane.get("bot_username") or ""
    rows = db.query_messages(lane["slug"], cursor, 500, "asc")
    incoming = [r for r in rows if not r["outgoing"]]
    for r in incoming:
        sender = (r.get("from") or "").lstrip("@").lower()
        if sender == bot_username.lstrip("@").lower():
            continue
        if telegram.mentions_bot(r.get("text") or "", bot_username):
            return r, incoming
    return None, incoming


@router.get("/{lane_slug}/wake")
async def wake(lane_slug: str, x_bridge_token: str = Header(default="")) -> dict:
    """Next @mention of this lane's bot the watcher hasn't handled yet.

    Detection and the cursor live server-side: the watcher stays stateless. On
    the first call the cursor is seeded to 'now' so history is never replayed.
    Returns the mention plus the lane's stored Claude session id to resume; the
    watcher acks via POST /wake/ack after running claude (which also reports the
    resulting session id back)."""
    lane = _auth_lane(lane_slug, x_bridge_token)
    session_id = db.get_lane_state(lane_slug, "claude_session_id")

    cursor_raw = db.get_lane_state(lane_slug, "wake_cursor")
    if cursor_raw is None:
        seed = db.max_incoming_update_id(lane_slug)
        db.set_lane_state(lane_slug, "wake_cursor", str(seed))
        return {"wake": False, "sessionId": session_id}
    cursor = int(cursor_raw)

    mention, incoming = _scan_mention(lane, cursor)
    if mention:
        return {
            "wake": True,
            "wakeId": mention["updateId"],
            "from": mention.get("from"),
            "text": mention.get("text"),
            "chatId": mention.get("chatId"),
            "sessionId": session_id,
            "mode": operator.lane_mode(lane_slug),
        }
    # No mention in this window: advance past the incoming messages we scanned
    # (never past outgoing high-namespace ids) so we don't rescan them.
    if incoming:
        db.set_lane_state(lane_slug, "wake_cursor", str(max(r["updateId"] for r in incoming)))
    return {"wake": False, "sessionId": session_id}


@router.post("/{lane_slug}/wake/ack")
async def wake_ack(lane_slug: str, req: WakeAck, x_bridge_token: str = Header(default="")) -> dict:
    """Mark a wake handled and record the (possibly forked) session id.

    Advancing the cursor to wakeId consumes that mention; storing sessionId is
    how the watcher reports which session to resume next time."""
    _auth_lane(lane_slug, x_bridge_token)
    db.set_lane_state(lane_slug, "wake_cursor", str(req.wake_id))
    if req.session_id:
        db.set_lane_state(lane_slug, "claude_session_id", req.session_id)
    return {"ok": True}


class LogEntry(BaseModel):
    level: str = Field(default="info", max_length=16)
    message: str = Field(min_length=1, max_length=2000)
    ts: int | None = None
    cost_usd: float | None = Field(default=None, alias="costUsd")

    model_config = {"populate_by_name": True}


@router.post("/{lane_slug}/log")
async def lane_log(lane_slug: str, req: LogEntry, x_bridge_token: str = Header(default="")) -> dict:
    """Append one watcher-activity line for this lane (rolling buffer, newest
    kept). The watcher self-reports with its own lane key; the admin panel reads
    every lane and the lane's owner reads their own — nobody reaches into a
    watcher, each pushes here. `costUsd` (on reply lines) feeds the 5h/weekly
    spend windows."""
    _auth_lane(lane_slug, x_bridge_token)
    db.add_lane_log(lane_slug, req.level, req.message, req.ts or int(time.time()), req.cost_usd or 0.0)
    return {"ok": True}


class DraftSubmit(BaseModel):
    wake_id: int = Field(alias="wakeId")
    text: str = Field(min_length=1, max_length=8000)
    cost_usd: float | None = Field(default=None, alias="costUsd")

    model_config = {"populate_by_name": True}


@router.post("/{lane_slug}/draft")
async def submit_draft(lane_slug: str, req: DraftSubmit, x_bridge_token: str = Header(default="")) -> dict:
    """Confirm-mode: the watcher submits the bot's draft reply. The hub posts a
    preview with Approve/Reject to the operator chat and holds it pending. If no
    operator console is configured, returns posted=False so the watcher can fall
    back to sending directly."""
    _auth_lane(lane_slug, x_bridge_token)
    posted = await operator.post_preview(lane_slug, req.wake_id, req.text)
    now = int(time.time())
    if not posted:
        return {"ok": True, "posted": False}
    op_chat, op_msg = posted
    db.create_approval(lane_slug, req.wake_id, req.text, req.cost_usd or 0.0, op_chat, op_msg, now)
    # cost is already logged on the watcher's run-summary line; keep this one cost-free
    db.add_lane_log(lane_slug, "info", "draft awaiting operator approval", now, 0.0)
    return {"ok": True, "posted": True}


@router.get("/{lane_slug}/draft")
async def draft_status(lane_slug: str, wakeId: int = Query(...), x_bridge_token: str = Header(default="")) -> dict:
    """Poll a submitted draft's decision: pending | approved | rejected | none."""
    _auth_lane(lane_slug, x_bridge_token)
    ap = db.get_approval(lane_slug, wakeId)
    return {"status": (ap or {}).get("status", "none")}


class OperatorNotice(BaseModel):
    text: str = Field(min_length=1, max_length=4000)

    model_config = {"populate_by_name": True}


@router.post("/{lane_slug}/operator")
async def operator_notice(lane_slug: str, req: OperatorNotice, x_bridge_token: str = Header(default="")) -> dict:
    """Post a start/finish/status notice to the operator chat via the console bot."""
    _auth_lane(lane_slug, x_bridge_token)
    return {"ok": await operator.notify(req.text)}


@router.get("/{lane_slug}/info")
async def info(lane_slug: str, x_bridge_token: str = Header(default="")) -> dict:
    lane = _auth_lane(lane_slug, x_bridge_token)
    mode = settings.resolved_delivery_mode()

    # Wake state — visible from the server, so you can see what the (remote,
    # stateless) watcher is working against without shelling into its machine.
    cursor_raw = db.get_lane_state(lane_slug, "wake_cursor")
    session_id = db.get_lane_state(lane_slug, "claude_session_id")
    pending: dict | None = None
    if cursor_raw is not None:
        mention, _ = _scan_mention(lane, int(cursor_raw))
        if mention:
            pending = {"wakeId": mention["updateId"], "from": mention.get("from")}

    return {
        "lane": lane["slug"],
        "botUsername": lane["bot_username"],
        "defaultChatId": lane["default_chat_id"] or None,
        "deliveryMode": mode,
        "webhookUrl": runtime.webhook_url(lane["slug"]) if mode == "webhook" else None,
        "polling": runtime.polling(lane["slug"]),
        "storedMessages": db.count_messages(lane["slug"]),
        "seenChats": db.seen_chats(lane["slug"]),
        "wake": {
            "armed": cursor_raw is not None,  # a watcher has polled at least once
            "cursor": int(cursor_raw) if cursor_raw is not None else None,
            "claudeSessionId": session_id,
            "pendingMention": pending,  # a mention waiting to be handled, if any
        },
    }


async def _handle_callback(update: dict) -> None:
    """Operator tapped Approve/Reject under a draft preview. callback_data is
    'ap:<slug>:<wakeId>' or 'rj:<slug>:<wakeId>'. Approve posts the draft to the
    team chat as the originating lane's bot; both edit the preview to show the
    outcome. The console bot is the one that posted the buttons and gets the tap."""
    cq = update.get("callback_query") or {}
    cb_id = cq.get("id")
    console = operator.console_lane()
    if not console:
        return
    bot = console["bot_token"]
    parts = (cq.get("data") or "").split(":")
    if len(parts) != 3:
        await telegram.answer_callback(bot, cb_id)
        return
    action, slug, wake_s = parts
    try:
        wake_id = int(wake_s)
    except ValueError:
        await telegram.answer_callback(bot, cb_id)
        return
    ap = db.get_approval(slug, wake_id)
    if not ap or ap["status"] != "pending":
        await telegram.answer_callback(bot, cb_id, "уже обработано")
        return
    now = int(time.time())
    op_chat, op_msg = ap.get("op_chat_id"), ap.get("op_message_id")
    draft = ap["draft"]
    if action == "ap":
        lane = _lane_or_404(slug)
        try:
            await perform_send(lane, draft, None)
        except Exception:
            await telegram.answer_callback(bot, cb_id, "ошибка отправки")
            return
        db.resolve_approval(slug, wake_id, "approved", now)
        db.add_lane_log(slug, "info", "operator approved → sent to chat", now)
        if op_msg:
            await telegram.edit_message(bot, op_chat, op_msg, f"✅ [{slug}] отправлено в чат:\n\n{draft[:3500]}")
        await telegram.answer_callback(bot, cb_id, "Отправлено")
    elif action == "rj":
        db.resolve_approval(slug, wake_id, "rejected", now)
        db.add_lane_log(slug, "info", "operator rejected draft", now)
        if op_msg:
            await telegram.edit_message(bot, op_chat, op_msg, f"✗ [{slug}] отклонено:\n\n{draft[:3500]}")
        await telegram.answer_callback(bot, cb_id, "Отклонено")
    else:
        await telegram.answer_callback(bot, cb_id)


async def _handle_operator_command(update: dict) -> bool:
    """`/status` typed in the operator chat -> the console bot replies with a
    per-lane spend + mode + pending-approval summary. Returns True if handled."""
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    if not text.startswith("/status"):
        return False
    cfg = operator.operator_config()
    console = operator.console_lane()
    if not console or str((msg.get("chat") or {}).get("id") or "") != cfg["chat_id"]:
        return False
    now = int(time.time())
    pend = {p["lane_slug"] for p in db.pending_approvals()}
    lines = ["📊 LaneHub — статус"]
    for l in db.list_lanes():
        if not l["enabled"]:
            continue
        w = db.spend_windows(l["slug"], now)
        flag = " ⏳ждёт апрув" if l["slug"] in pend else ""
        lines.append(f"• {l['slug']} [{operator.lane_mode(l['slug'])}] — "
                     f"5ч ${w['h5']['usd']:.2f}/{w['h5']['requests']} · 7д ${w['week']['usd']:.2f}{flag}")
    allw = db.spend_windows(None, now)
    lines.append(f"Σ все боты: 5ч ${allw['h5']['usd']:.2f} ({allw['h5']['requests']}) · 7д ${allw['week']['usd']:.2f}")
    await telegram.send_message(console["bot_token"], cfg["chat_id"], "\n".join(lines))
    return True


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
    if not isinstance(update, dict):
        return {"ok": True}
    if "callback_query" in update:
        await _handle_callback(update)
        return {"ok": True}
    if await _handle_operator_command(update):
        return {"ok": True}
    if "update_id" in update:
        ingest_update(lane_slug, update)
    return {"ok": True}
