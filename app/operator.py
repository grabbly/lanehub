"""Human-in-the-loop operator console — PER LANE.

Each lane (bot) has its OWN operator chat (a separate Telegram chat the bot's
owner sets up), stored per lane as `operator_chat_id`. The lane's OWN bot is the
console: it posts that lane's start/finish/status and draft previews (with
Approve/Reject buttons) into that lane's operator chat, and receives the taps on
its own webhook. An operator only ever sees their own bot — nothing about other
lanes. Approved drafts go to the team chat as the same bot.

Kept free of route imports so it never creates an import cycle.
"""
from __future__ import annotations

from . import db, telegram


def lane_operator_chat(lane_slug: str) -> str:
    """The operator chat id set for this lane, or '' if the owner hasn't set one."""
    return db.get_lane_state(lane_slug, "operator_chat_id") or ""


def lane_mode(lane_slug: str) -> str:
    """'confirm' (draft gated through this lane's operator chat) or 'auto' (post
    directly). Confirm silently falls back to auto when this lane has no operator
    chat configured."""
    mode = (db.get_lane_state(lane_slug, "reply_mode") or "auto").strip()
    if mode == "confirm" and not lane_operator_chat(lane_slug):
        return "auto"
    return mode


def _ready(lane_slug: str) -> tuple[str, dict] | None:
    chat = lane_operator_chat(lane_slug)
    lane = db.get_lane(lane_slug)
    if not chat or not lane or not lane.get("enabled"):
        return None
    return chat, lane


async def notify(lane_slug: str, text: str) -> bool:
    """Best-effort line to THIS lane's operator chat via its own bot."""
    ready = _ready(lane_slug)
    if not ready:
        return False
    chat, lane = ready
    try:
        await telegram.send_message(lane["bot_token"], chat, text[:4000])
        return True
    except telegram.TelegramError:
        return False


async def post_preview(lane_slug: str, wake_id: int, draft: str) -> tuple[str, int] | None:
    """Post a draft preview with Approve/Reject buttons to THIS lane's operator
    chat via its own bot. Returns (op_chat_id, op_message_id) or None."""
    ready = _ready(lane_slug)
    if not ready:
        return None
    chat, lane = ready
    text = f"✎ черновик ответа — подтверди, чтобы отправить в чат:\n\n{draft[:3500]}"
    buttons = [("✓ Отправить", f"ap:{lane_slug}:{wake_id}"),
               ("✗ Отклонить", f"rj:{lane_slug}:{wake_id}")]
    try:
        msg = await telegram.send_buttons(lane["bot_token"], chat, text, buttons)
    except telegram.TelegramError:
        return None
    return chat, int(msg.get("message_id") or 0)
