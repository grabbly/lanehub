"""Human-in-the-loop operator console over a dedicated Telegram chat.

One shared operator chat (hub setting `operator_chat_id`) plus one designated
"console" lane (`operator_console_slug`) whose bot is the console's voice: it
posts start/finish/status notices and draft previews (with Approve/Reject
buttons) for EVERY lane, and receives the button taps on its own webhook.
Approved drafts are posted to the team chat as the *originating* lane's bot.

Kept free of route imports so it never creates an import cycle.
"""
from __future__ import annotations

from . import db, telegram


def operator_config() -> dict:
    return {
        "chat_id": db.get_hub_state("operator_chat_id") or "",
        "console_slug": db.get_hub_state("operator_console_slug") or "",
    }


def console_lane() -> dict | None:
    """The enabled lane whose bot speaks in the operator chat, or None if the
    operator console isn't configured."""
    cfg = operator_config()
    if not cfg["chat_id"] or not cfg["console_slug"]:
        return None
    lane = db.get_lane(cfg["console_slug"])
    return lane if lane and lane.get("enabled") else None


def lane_mode(lane_slug: str) -> str:
    """'confirm' (draft gated through the operator) or 'auto' (post directly).
    Confirm silently falls back to auto when no console is configured."""
    mode = (db.get_lane_state(lane_slug, "reply_mode") or "auto").strip()
    if mode == "confirm" and console_lane() is None:
        return "auto"
    return mode


async def notify(text: str) -> bool:
    """Best-effort line to the operator chat via the console bot."""
    cfg = operator_config()
    lane = console_lane()
    if not lane:
        return False
    try:
        await telegram.send_message(lane["bot_token"], cfg["chat_id"], text[:4000])
        return True
    except telegram.TelegramError:
        return False


async def post_preview(lane_slug: str, wake_id: int, draft: str) -> tuple[str, int] | None:
    """Post a draft preview with Approve/Reject buttons. Returns
    (op_chat_id, op_message_id) or None if the console isn't configured."""
    cfg = operator_config()
    console = console_lane()
    if not console:
        return None
    text = f"✎ [{lane_slug}] черновик ответа — подтверди, чтобы отправить в чат:\n\n{draft[:3500]}"
    buttons = [("✓ Отправить", f"ap:{lane_slug}:{wake_id}"),
               ("✗ Отклонить", f"rj:{lane_slug}:{wake_id}")]
    try:
        msg = await telegram.send_buttons(console["bot_token"], cfg["chat_id"], text, buttons)
    except telegram.TelegramError:
        return None
    return cfg["chat_id"], int(msg.get("message_id") or 0)
