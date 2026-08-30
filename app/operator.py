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
