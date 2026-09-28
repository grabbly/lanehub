"""Per-lane delivery runtime.

Keeps each enabled lane wired to Telegram in the configured delivery mode:

- webhook: registers `{public_base_url}/{slug}/webhook` with the lane's secret;
  Telegram pushes updates, no background task needed.
- polling: runs one asyncio getUpdates long-poll task per lane (works without
  a public URL — laptops, NAT'd VPS, local testing).
- off: no Telegram delivery at all (tests).

`sync_lane` is called at startup for every lane and again whenever a lane is
created/updated/deleted from the admin API, so the runtime always reflects the
database. A lane whose bot's updates belong to another system (`send_only`)
is never touched: the hub doesn't set, delete or poll its webhook.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from . import db, telegram
from .config import settings

LOG = logging.getLogger("lanehub.runtime")


def ingest_update(lane_slug: str, upd: dict) -> None:
    """Persist one Telegram update (poller or webhook). No-op without a message."""
    msg = upd.get("message") or upd.get("channel_post")
    if not msg:
        return
    chat = msg.get("chat", {})
    if msg.get("migrate_to_chat_id") and chat.get("id") is not None:
        # The group became a supergroup under a new id: follow it, or every
        # later /send would fail with "chat not found".
        moved = db.migrate_chat(chat["id"], msg["migrate_to_chat_id"])
        if moved:
            LOG.info("chat %s migrated to %s; rebound lanes %s", chat["id"], msg["migrate_to_chat_id"], moved)
    db.store_message(
        lane_slug=lane_slug,
        update_id=upd["update_id"],
        message_id=msg.get("message_id"),
        chat_id=chat.get("id"),
        chat_title=chat.get("title") or chat.get("username"),
        from_user=telegram.extract_sender(msg),
        text=telegram.extract_text(msg),
        date=msg.get("date", 0),
        is_outgoing=False,
        media=telegram.extract_media(msg),
        **telegram.extract_author(msg),
    )


async def canonicalize_binding(lane: dict) -> dict:
    """Turn a lane bound by @channelname (pre-0.5 bindings) into its numeric
    chat id, which the /feed visibility filter and group-upgrade tracking need.
    The handle is kept as `chat_alias`, so an agent that still sends
    chatId="@name" isn't refused. Best effort: if Telegram can't be asked now
    the lane is returned unchanged and the next call retries."""
    bound = (lane.get("default_chat_id") or "").strip()
    if not bound or telegram.is_numeric_chat_id(bound):
        return lane
    try:
        chat = await telegram.resolve_chat(lane["bot_token"], bound)
    except telegram.TelegramError as exc:
        LOG.warning("lane %s: cannot resolve bound chat %s: %s", lane["slug"], bound, exc)
        return lane
    if chat.get("id") is None:
        return lane
    db.set_lane_state(lane["slug"], "chat_alias", bound)
    LOG.info("lane %s: bound chat %s resolved to %s", lane["slug"], bound, chat["id"])
    return db.update_lane(lane["slug"], {"default_chat_id": str(chat["id"])}) or lane


class LaneRuntime:
    def __init__(self) -> None:
        self._pollers: dict[str, asyncio.Task] = {}

    def webhook_url(self, slug: str) -> str:
        return f"{settings.public_base_url}/{slug}/webhook"

    async def sync_all(self) -> None:
        for lane in db.list_lanes():
            await self.sync_lane(await canonicalize_binding(lane))

    def is_own_webhook(self, slug: str, url: str | None) -> bool:
        """True when `url` is this lane's hub webhook — at the current address,
        or at a previous address of the hub (same `/{slug}/webhook` path, e.g.
        after a domain move). An empty url is nobody's."""
        if not url:
            return False
        if url == self.webhook_url(slug):
            return True
        return urlsplit(url).path.rstrip("/").endswith(f"/{slug}/webhook")

    async def foreign_webhook(self, lane: dict) -> str | None:
        """The bot's webhook URL if another system (not this hub) receives its
        updates, else None. Raises TelegramError when Telegram can't be asked."""
        info = await telegram.get_webhook_info(lane["bot_token"])
        url = (info or {}).get("url") or ""
        if url and not self.is_own_webhook(lane["slug"], url):
            return url
        return None

    async def sync_lane(self, lane: dict, take_over: bool = False) -> str | None:
        """Bring one lane's delivery in line with its DB row.

        A Telegram bot has ONE webhook, so setting ours takes the bot's updates
        (including users' DMs) away from whatever received them before. The hub
        therefore never touches the webhook of a `send_only` lane, and before
        setting or deleting it for a `hub` lane it checks who owns it: if
        another system does, the lane is switched to `send_only` instead —
        unless `take_over` (the operator explicitly chose "hub receives").

        Returns a warning string when something needs the operator's attention
        (the lane row itself is already saved) so callers can surface it."""
        slug = lane["slug"]
        self._stop_poller(slug)
        mode = settings.resolved_delivery_mode()
        if mode == "off" or lane.get("receive_mode") == "send_only":
            return None
        if not take_over:
            try:
                foreign = await self.foreign_webhook(lane)
            except telegram.TelegramError as exc:
                LOG.warning("lane %s: getWebhookInfo failed, webhook left as is: %s", slug, exc)
                return f"could not check the bot's webhook ({exc}) — left it untouched; save the lane again later"
            if foreign:
                db.update_lane(slug, {"receive_mode": "send_only"})
                db.delete_lane_state(slug, "wake_cursor")  # mentions are now found via the chat feed
                LOG.warning("lane %s: bot's updates go to %s — switched to send-only", slug, foreign)
                return (f"this bot's updates already go to {foreign} — the lane was set to send-only so that "
                        "system keeps working. The bot still posts through the hub and reads the chat via "
                        "the other lanes' bots.")
        if not lane.get("enabled"):
            try:
                await telegram.delete_webhook(lane["bot_token"])
            except telegram.TelegramError as exc:
                LOG.warning("lane %s: deleteWebhook failed: %s", slug, exc)
            return None
        if mode == "webhook":
            if not settings.public_base_url:
                return "webhook mode but HUB_PUBLIC_BASE_URL is empty — lane will receive nothing"
            try:
                await telegram.set_webhook(lane["bot_token"], self.webhook_url(slug), lane["webhook_secret"])
            except telegram.TelegramError as exc:
                LOG.warning("lane %s: setWebhook failed: %s", slug, exc)
                return f"setWebhook failed: {exc}"
            return None
        # polling
        try:
            await telegram.delete_webhook(lane["bot_token"])
        except telegram.TelegramError as exc:
            LOG.warning("lane %s: deleteWebhook failed (poller may 409): %s", slug, exc)
        self._pollers[slug] = asyncio.create_task(self._poll_loop(slug), name=f"poller:{slug}")
        return None

    async def remove_lane(self, lane: dict) -> None:
        """Stop delivery for a lane being deleted. Only a webhook that is ours
        is removed — never another system's."""
        self._stop_poller(lane["slug"])
        if settings.resolved_delivery_mode() == "off" or lane.get("receive_mode") == "send_only":
            return
        try:
            if await self.foreign_webhook(lane):
                return
            await telegram.delete_webhook(lane["bot_token"])
        except telegram.TelegramError as exc:
            LOG.warning("lane %s: deleteWebhook on remove failed: %s", lane["slug"], exc)

    async def stop_all(self) -> None:
        for slug in list(self._pollers):
            self._stop_poller(slug)

    def polling(self, slug: str) -> bool:
        task = self._pollers.get(slug)
        return bool(task and not task.done())

    def _stop_poller(self, slug: str) -> None:
        task = self._pollers.pop(slug, None)
        if task:
            task.cancel()

    async def _poll_loop(self, slug: str) -> None:
        LOG.info("poller started for lane %s (interval=%.1fs)", slug, settings.poll_interval)
        while True:
            try:
                await self._poll_once(slug)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("lane %s: poll error: %s", slug, exc)
            await asyncio.sleep(settings.poll_interval)

    async def _poll_once(self, slug: str) -> None:
        lane = db.get_lane(slug)
        if not lane or not lane["enabled"] or lane.get("receive_mode") == "send_only":
            return
        offset_raw = db.get_lane_state(slug, "next_offset")
        offset = int(offset_raw) if offset_raw is not None else None
        updates = await telegram.get_updates(lane["bot_token"], offset)
        if not updates:
            return
        for upd in updates:
            ingest_update(slug, upd)
        db.set_lane_state(slug, "next_offset", str(updates[-1]["update_id"] + 1))
        LOG.info("lane %s: ingested %d updates", slug, len(updates))


runtime = LaneRuntime()
