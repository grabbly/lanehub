"""Autopilot mode logic for LaneHub (0.8.0).

Automates @mention replies without any always-running software on the bot owner's
computer. The watcher runs only while autopilot is ON and exits by itself when OFF.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from . import db, operator, telegram

LOG = logging.getLogger("lanehub.autopilot")

AUTO_PREFIX = "🤖 auto · "
WATCHER_TIMEOUT = 120  # seconds without /wake poll before deemed offline
DEFAULT_HOURS = 8.0
MAX_HOURS = 72.0

AUTOPILOT_CMD_RE = re.compile(
    r"^/autopilot(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?$",
    re.IGNORECASE,
)
# One tap from the chat's command menu: /autopilot_on_<handle> and
# /autopilot_off_<handle> (Telegram may append @<bot_username>).
AUTOPILOT_BOT_CMD_RE = re.compile(
    r"^/autopilot_(on|off)_([a-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s.*)?$",
    re.IGNORECASE | re.DOTALL,
)
_CMD_MAX = 32  # Telegram's limit for a bot command


def command_handle(bot_username: str) -> str:
    """The bot's part of its chat commands: '@Creativeactive_bot' -> 'creativeactive',
    trimmed so 'autopilot_off_<handle>' fits Telegram's 32-character limit."""
    base = re.sub(r"[^a-z0-9_]", "", (bot_username or "").lower())
    for suffix in ("_bot", "bot"):
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
            break
    return base.strip("_")[: _CMD_MAX - len("autopilot_off_")] or "bot"


def bot_commands(lane: dict) -> list[dict]:
    """The two commands a hub-mode lane registers in its bound chat."""
    handle = command_handle(lane.get("bot_username") or lane["slug"])
    bot_tag = f"@{lane.get('bot_username') or lane['slug']}"
    return [
        {"command": f"autopilot_on_{handle}", "description": f"Ask the owner to switch on autopilot for {bot_tag}"},
        {"command": f"autopilot_off_{handle}", "description": f"Switch off autopilot for {bot_tag}"},
    ]


def record_outgoing(lane: dict, result: dict, text: str, media: dict | None = None) -> dict:
    """Store a sent message as a synthetic outgoing row so other lanes' readers
    see it in /feed (Telegram never delivers a bot's messages to other bots)."""
    db.set_lane_state(lane["slug"], "last_send_ok", str(int(time.time())))
    db.set_lane_state(lane["slug"], "last_send_error", "")
    chat = result.get("chat", {})
    sender = result.get("from", {})
    return db.store_message(
        lane_slug=lane["slug"],
        update_id=None,  # takes the hub-wide seq
        message_id=result.get("message_id"),
        chat_id=chat.get("id"),
        chat_title=chat.get("title") or chat.get("username"),
        from_user=sender.get("username") or lane["bot_username"] or lane["slug"],
        text=text,
        date=result.get("date") or int(time.time()),
        is_outgoing=True,
        media=media,
        from_id=sender.get("id") or telegram.bot_id_from_token(lane["bot_token"]),
        from_username=sender.get("username") or lane["bot_username"] or None,
        from_is_bot=True,
    )


def is_autopilot_command(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    return bool(AUTOPILOT_CMD_RE.match(t) or AUTOPILOT_BOT_CMD_RE.match(t))


def parse_autopilot_command(text: str) -> dict | None:
    if not text:
        return None
    m = AUTOPILOT_BOT_CMD_RE.match(text.strip())
    if m:
        return {"handle": m.group(2).lower(), "bot_username": None, "action": m.group(1).lower()}
    m = AUTOPILOT_CMD_RE.match(text.strip())
    if not m:
        return None
    inline_bot = m.group(1)
    remainder = (m.group(2) or "").strip()
    tokens = remainder.split()

    target_bot = inline_bot
    action = None

    for tok in tokens:
        if tok.startswith("@") and not target_bot:
            target_bot = tok[1:]
        elif tok.lower() in ("on", "off", "status"):
            action = tok.lower()

    if not action:
        action = "on"  # default action is "on" (e.g. /autopilot @bot)

    return {
        "bot_username": target_bot,
        "action": action,
    }


def is_autopilot_on(lane_slug: str, now: int | None = None) -> bool:
    if now is None:
        now = int(time.time())
    until_raw = db.get_lane_state(lane_slug, "autopilot_until")
    if not until_raw:
        return False
    try:
        return now < int(until_raw)
    except ValueError:
        return False


def get_autopilot_state(lane_slug: str, now: int | None = None) -> dict:
    if now is None:
        now = int(time.time())
    on = is_autopilot_on(lane_slug, now)
    until_raw = db.get_lane_state(lane_slug, "autopilot_until")
    until = int(until_raw) if (on and until_raw and until_raw.isdigit()) else None
    by = db.get_lane_state(lane_slug, "autopilot_by")
    owner = db.get_lane_state(lane_slug, "autopilot_owner")
    seen_raw = db.get_lane_state(lane_slug, "watcher_seen")
    seen = int(seen_raw) if (seen_raw and seen_raw.isdigit()) else None
    online = bool(seen is not None and (now - seen) < WATCHER_TIMEOUT)
    return {
        "on": on,
        "until": until,
        "by": by or None,
        "owner": owner or None,
        "watcherSeen": seen,
        "watcherOnline": online,
    }


async def set_autopilot_on(
    lane: dict,
    hours: float = DEFAULT_HOURS,
    by: str = "agent",
    now: int | None = None,
) -> tuple[int, str]:
    if now is None:
        now = int(time.time())
    hours = max(0.01, min(float(hours or DEFAULT_HOURS), MAX_HOURS))
    until = int(now + hours * 3600)
    slug = lane["slug"]
    db.set_lane_state(slug, "autopilot_until", str(until))
    db.set_lane_state(slug, "autopilot_by", by)
    db.set_lane_state(slug, "autopilot_started_at", str(now))

    bot_user = lane.get("bot_username")
    bot_tag = f"@{bot_user}" if bot_user else f"/{slug}"
    hh_mm = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%H:%M UTC")
    off_cmd = bot_commands(lane)[1]["command"]
    announcement = (f"🤖 Autopilot ON for {bot_tag} until {hh_mm}. Mentions get automatic replies, "
                    f"marked 🤖 auto. Anyone can switch it off: /{off_cmd}")

    chat_id = (lane.get("default_chat_id") or "").strip()
    if chat_id:
        try:
            res = await telegram.send_message(lane["bot_token"], chat_id, announcement)
            record_outgoing(lane, res, announcement)
        except Exception as exc:
            LOG.warning("lane %s: failed to announce autopilot ON: %s", slug, exc)

    return until, announcement


async def set_autopilot_off(
    lane: dict,
    reason: str,
    now: int | None = None,
) -> str:
    if now is None:
        now = int(time.time())
    slug = lane["slug"]
    until_raw = db.get_lane_state(slug, "autopilot_until")
    if not until_raw:
        return ""
    try:
        if int(until_raw) <= 0:
            return ""
    except ValueError:
        return ""

    db.set_lane_state(slug, "autopilot_until", "0")
    db.set_lane_state(slug, "autopilot_by", reason)

    bot_user = lane.get("bot_username")
    bot_tag = f"@{bot_user}" if bot_user else f"/{slug}"
    announcement = f"🤖 Autopilot OFF for {bot_tag} ({reason})."

    chat_id = (lane.get("default_chat_id") or "").strip()
    if chat_id:
        try:
            res = await telegram.send_message(lane["bot_token"], chat_id, announcement)
            record_outgoing(lane, res, announcement)
        except Exception as exc:
            LOG.warning("lane %s: failed to announce autopilot OFF: %s", slug, exc)

    return announcement


async def handle_incoming_update_commands(receiving_slug: str, update: dict) -> bool:
    """Handle /autopilot chat commands in bound chats for hub-mode lanes."""
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return False
    text = (msg.get("text") or "").strip()
    if not text:
        return False

    cmd = parse_autopilot_command(text)
    if not cmd:
        return False

    chat_id = (msg.get("chat") or {}).get("id")
    if chat_id is None:
        return False

    # Find all hub-mode enabled lanes bound to this chat
    bound_hub_lanes = [
        l for l in db.list_lanes()
        if l.get("enabled")
        and l.get("receive_mode") != "send_only"
        and str(l.get("default_chat_id") or "").strip() == str(chat_id)
    ]
    if not bound_hub_lanes:
        return False

    target_bot = cmd.get("bot_username")
    target_lane = None
    if cmd.get("handle"):
        target_lane = next(
            (l for l in bound_hub_lanes
             if command_handle(l.get("bot_username") or l["slug"]) == cmd["handle"]),
            None,
        )
        if not target_lane:
            return False
    elif target_bot:
        for l in bound_hub_lanes:
            if (l.get("bot_username") or "").lower() == target_bot.lower():
                target_lane = l
                break
        if not target_lane:
            return False
    else:
        # The @<bot> may be omitted only if this lane is the only lane bound to that chat
        if len(bound_hub_lanes) == 1:
            target_lane = bound_hub_lanes[0]
        else:
            return False

    # Several of the hub's bots may sit in this chat with privacy off: each
    # receives the same command. Handle it once.
    seen_key = f"{chat_id}:{msg.get('message_id')}"
    if db.get_lane_state(target_lane["slug"], "last_autopilot_cmd") == seen_key:
        return True
    db.set_lane_state(target_lane["slug"], "last_autopilot_cmd", seen_key)

    action = cmd.get("action") or "on"
    bot_user = target_lane.get("bot_username")
    bot_tag = f"@{bot_user}" if bot_user else f"/{target_lane['slug']}"
    now = int(time.time())

    if action == "on":
        if is_autopilot_on(target_lane["slug"], now):
            # Already ON -> reply with status
            until_ts = int(db.get_lane_state(target_lane["slug"], "autopilot_until") or 0)
            hh_mm = datetime.fromtimestamp(until_ts, tz=timezone.utc).strftime("%H:%M UTC")
            reply = f"🤖 Autopilot ON for {bot_tag} until {hh_mm}. Mentions get automatic replies, marked 🤖 auto."
            try:
                res = await telegram.send_message(target_lane["bot_token"], str(chat_id), reply)
                record_outgoing(target_lane, res, reply)
            except Exception as exc:
                LOG.warning("lane %s: failed to reply status: %s", target_lane["slug"], exc)
            return True

        # Currently OFF -> ask owner
        owner = db.get_lane_state(target_lane["slug"], "autopilot_owner")
        frm = msg.get("from") or {}
        sender_username = frm.get("username")
        sender_tag = f"@{sender_username}" if sender_username else telegram.extract_sender(msg)

        if owner:
            reply = (
                f"🤖 @{owner}, {sender_tag} asks to switch on autopilot for {bot_tag}. "
                f"Press Start in LaneHub Autopilot or ask your Claude to switch it on."
            )
            try:
                res = await telegram.send_message(target_lane["bot_token"], str(chat_id), reply)
                record_outgoing(target_lane, res, reply)
            except Exception as exc:
                LOG.warning("lane %s: failed to reply to chat: %s", target_lane["slug"], exc)

            # If the lane has an operator/debug chat, also send the request there
            await operator.notify(target_lane["slug"], reply)
        else:
            reply = f"🤖 {sender_tag} asks to switch on autopilot for {bot_tag}, but no owner is set in the LaneHub panel."
            try:
                res = await telegram.send_message(target_lane["bot_token"], str(chat_id), reply)
                record_outgoing(target_lane, res, reply)
            except Exception as exc:
                LOG.warning("lane %s: failed to reply to chat: %s", target_lane["slug"], exc)
        return True

    elif action == "off":
        if is_autopilot_on(target_lane["slug"], now):
            frm = msg.get("from") or {}
            sender_username = frm.get("username")
            sender_tag = f"@{sender_username}" if sender_username else telegram.extract_sender(msg)
            await set_autopilot_off(target_lane, reason=f"by {sender_tag}", now=now)
        return True

    elif action == "status":
        if is_autopilot_on(target_lane["slug"], now):
            until_ts = int(db.get_lane_state(target_lane["slug"], "autopilot_until") or 0)
            hh_mm = datetime.fromtimestamp(until_ts, tz=timezone.utc).strftime("%H:%M UTC")
            reply = f"🤖 Autopilot ON for {bot_tag} until {hh_mm}. Mentions get automatic replies, marked 🤖 auto."
        else:
            reply = f"🤖 Autopilot OFF for {bot_tag}."
        try:
            res = await telegram.send_message(target_lane["bot_token"], str(chat_id), reply)
            record_outgoing(target_lane, res, reply)
        except Exception as exc:
            LOG.warning("lane %s: failed to reply status: %s", target_lane["slug"], exc)
        return True

    return False


async def autopilot_check_pass(now: int | None = None) -> list[dict]:
    """One pass of background autopilot check:
    - ON past until -> OFF + announce (timer)
    - ON and watcher_seen older than 120s -> OFF + announce (computer went offline)
    """
    if now is None:
        now = int(time.time())
    actions = []
    for lane in db.list_lanes():
        if not lane.get("enabled"):
            continue
        slug = lane["slug"]
        until_raw = db.get_lane_state(slug, "autopilot_until")
        if not until_raw:
            continue
        try:
            until = int(until_raw)
        except ValueError:
            continue
        if until <= 0:
            continue

        started_raw = db.get_lane_state(slug, "autopilot_started_at")
        started = int(started_raw or 0)
        seen_raw = db.get_lane_state(slug, "watcher_seen")
        seen = int(seen_raw) if seen_raw else None

        # 1. Timer expiry
        if now >= until:
            await set_autopilot_off(lane, reason="timer", now=now)
            actions.append({"lane": slug, "action": "off", "reason": "timer"})
            continue

        # 2. Watcher went offline (no /wake poll for 120s while ON). While it
        # is answering a mention it runs claude and doesn't poll — that's busy,
        # not offline (the window closes on /wake/ack or after 30 min).
        if int(db.get_lane_state(slug, "auto_window_until") or 0) > now:
            continue
        last_activity = max(seen or 0, started)
        if (now - last_activity) >= WATCHER_TIMEOUT:
            await set_autopilot_off(lane, reason="computer went offline", now=now)
            actions.append({"lane": slug, "action": "off", "reason": "computer went offline"})
            continue
    return actions
