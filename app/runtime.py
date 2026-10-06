"""Per-bot delivery runtime.

Keeps every bot the hub receives for wired to Telegram in the configured
delivery mode:

- webhook: registers `{public_base_url}/{slug}/webhook` with the lane's secret;
  Telegram pushes updates, no background task needed.
- polling: runs one asyncio getUpdates long-poll task per bot (works without
  a public URL — laptops, NAT'd VPS, local testing).
- off: no Telegram delivery at all (tests).

A Telegram bot has ONE webhook, but one bot may serve several lanes — one per
chat it sits in. The hub registers the webhook once, at the bot's *receiver*
lane (its oldest enabled lane), and `ingest_update` files each update under
the lane bound to the update's chat. So the same bot can be added to any
number of chats, each with its own lane, key and history.

`sync_lane` is called at startup for every lane and again whenever a lane is
created/updated/deleted from the admin API, so the runtime always reflects the
database. A bot whose updates belong to another system (`send_only`) is never
touched: the hub doesn't set, delete or poll its webhook.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from . import db, telegram
from .autopilot import autopilot_check_pass, handle_incoming_update_commands
from .config import settings

LOG = logging.getLogger("lanehub.runtime")


def route_update(lane_slug: str, chat_id: int | None) -> list[str]:
    """Lanes an update received by `lane_slug`'s bot belongs to: every lane of
    that bot bound to the update's chat. A chat none of them is bound to (a DM,
    a chat the bot was just added to) stays with the receiving lane, so it
    shows up under the bot's seen chats, ready to be bound."""
    lane = db.get_lane(lane_slug)
    if not lane or chat_id is None:
        return [lane_slug]
    bound = [
        other["slug"] for other in db.bot_lanes(lane["bot_token"])
        if (other["default_chat_id"] or "").strip() == str(chat_id)
    ]
    return bound or [lane_slug]


def ingest_update(lane_slug: str, upd: dict) -> None:
    """Persist one Telegram update (poller or webhook) under the lane(s) bound
    to its chat. No-op without a message."""
    msg = upd.get("message") or upd.get("channel_post")
    if not msg:
        return
    chat = msg.get("chat", {})
    targets = route_update(lane_slug, chat.get("id"))  # before a migration rebinds the chat
    if msg.get("migrate_to_chat_id") and chat.get("id") is not None:
        # The group became a supergroup under a new id: follow it, or every
        # later /send would fail with "chat not found".
        moved = db.migrate_chat(chat["id"], msg["migrate_to_chat_id"])
        if moved:
            LOG.info("chat %s migrated to %s; rebound lanes %s", chat["id"], msg["migrate_to_chat_id"], moved)
    for slug in targets:
        db.store_message(
            lane_slug=slug,
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
        self._autopilot_task: asyncio.Task | None = None

    def webhook_url(self, slug: str) -> str:
        return f"{settings.public_base_url}/{slug}/webhook"

    async def sync_all(self) -> None:
        if self._autopilot_task is None or self._autopilot_task.done():
            self._autopilot_task = asyncio.create_task(self._autopilot_loop(), name="autopilot_check")
        for lane in db.list_lanes():
            await canonicalize_binding(lane)
        done: set[str] = set()
        for lane in db.list_lanes():
            if lane["bot_token"] not in done:
                done.add(lane["bot_token"])
                await self.sync_bot(lane["bot_token"])

    def hub_lane_of(self, url: str | None, slugs: list[str]) -> str | None:
        """Which of `slugs` the webhook `url` belongs to — at the hub's current
        address, or at a previous one (same `/{slug}/webhook` path, e.g. after
        a domain move). None for an empty or foreign url."""
        if not url:
            return None
        path = urlsplit(url).path.rstrip("/")
        for slug in slugs:
            if url == self.webhook_url(slug) or path.endswith(f"/{slug}/webhook"):
                return slug
        return None

    def is_own_webhook(self, lane: dict, url: str | None) -> bool:
        """True when `url` is this hub's webhook for the lane's bot — at this
        lane or at another lane of the same bot."""
        slugs = {lane["slug"], *(other["slug"] for other in db.bot_lanes(lane["bot_token"]))}
        return self.hub_lane_of(url, sorted(slugs)) is not None

    def receiver(self, bot_token: str) -> dict | None:
        """The lane whose webhook URL receives this bot's updates for all of its
        lanes: the oldest enabled one. None if the bot is send-only here or
        every lane of it is disabled."""
        lanes = db.bot_lanes(bot_token)
        if any(lane.get("receive_mode") == "send_only" for lane in lanes):
            return None
        return next((lane for lane in lanes if lane["enabled"]), None)

    async def foreign_webhook(self, bot_token: str, slugs: list[str]) -> str | None:
        """The bot's webhook URL if another system (not this hub, at none of
        `slugs`) receives its updates, else None. Raises TelegramError when
        Telegram can't be asked."""
        info = await telegram.get_webhook_info(bot_token)
        url = (info or {}).get("url") or ""
        if url and self.hub_lane_of(url, slugs) is None:
            return url
        return None

    async def sync_lane(self, lane: dict, take_over: bool = False) -> str | None:
        """Bring the delivery of the lane's bot in line with the DB."""
        return await self.sync_bot(lane["bot_token"], take_over=take_over)

    async def sync_bot(self, bot_token: str, take_over: bool = False,
                       released: tuple[str, ...] = ()) -> str | None:
        """Bring one bot's delivery in line with its lanes' DB rows.

        A Telegram bot has ONE webhook, so setting ours takes the bot's updates
        (including users' DMs) away from whatever received them before. The hub
        therefore never touches the webhook of a `send_only` bot, and before
        setting or deleting it checks who owns it: if another system does, the
        bot's lanes are switched to `send_only` instead — unless `take_over`
        (the operator explicitly chose "hub receives").

        Lanes of one bot share its webhook: it is registered once, at the
        receiver lane, and each update is filed under the lane bound to its
        chat. When the hub owns the webhook, none of the bot's lanes needs to be
        send-only (0.5.4/0.5.5 made all but one so) — they are set back to hub.

        `released`: slugs of this bot's lanes just deleted — a webhook still
        pointing at one of them is ours, not another system's.

        Returns a warning string when something needs the operator's attention
        (the lane rows themselves are already saved) so callers can surface it."""
        lanes = db.bot_lanes(bot_token)
        for slug in [*(lane["slug"] for lane in lanes), *released]:
            self._stop_poller(slug)
        mode = settings.resolved_delivery_mode()
        hub_lanes = [lane for lane in lanes if lane.get("receive_mode") != "send_only"]
        if mode == "off" or not hub_lanes:
            return None
        slugs = [*(lane["slug"] for lane in lanes), *released]
        if not take_over:
            try:
                foreign = await self.foreign_webhook(bot_token, slugs)
            except telegram.TelegramError as exc:
                LOG.warning("lanes %s: getWebhookInfo failed, webhook left as is: %s", slugs, exc)
                return f"could not check the bot's webhook ({exc}) — left it untouched; save the lane again later"
            if foreign:
                for lane in hub_lanes:
                    db.update_lane(lane["slug"], {"receive_mode": "send_only"})
                    db.delete_lane_state(lane["slug"], "wake_cursor")  # mentions are now found via the chat feed
                LOG.warning("lanes %s: bot's updates go to %s — switched to send-only",
                            [lane["slug"] for lane in hub_lanes], foreign)
                return (f"this bot's updates already go to {foreign} — the lane was set to send-only so that "
                        "system keeps working. The bot still posts through the hub and reads the chat via "
                        "the other lanes' bots.")
        for lane in lanes:
            if lane.get("receive_mode") == "send_only":
                db.update_lane(lane["slug"], {"receive_mode": "hub"})
                db.delete_lane_state(lane["slug"], "wake_cursor")  # hub mode counts in update_ids
                LOG.info("lane %s: the hub receives its bot's updates — back to hub", lane["slug"])
        receiver = self.receiver(bot_token)
        if receiver is None:  # every lane of this bot is disabled
            try:
                await telegram.delete_webhook(bot_token)
            except telegram.TelegramError as exc:
                LOG.warning("lanes %s: deleteWebhook failed: %s", slugs, exc)
            return None
        slug = receiver["slug"]
        if mode == "webhook":
            if not settings.public_base_url:
                return "webhook mode but HUB_PUBLIC_BASE_URL is empty — lane will receive nothing"
            try:
                await telegram.set_webhook(bot_token, self.webhook_url(slug), receiver["webhook_secret"])
            except telegram.TelegramError as exc:
                LOG.warning("lane %s: setWebhook failed: %s", slug, exc)
                return f"setWebhook failed: {exc}"
            return None
        # polling: one poller per bot (a second getUpdates on the same token gets 409)
        try:
            await telegram.delete_webhook(bot_token)
        except telegram.TelegramError as exc:
            LOG.warning("lane %s: deleteWebhook failed (poller may 409): %s", slug, exc)
        self._pollers[slug] = asyncio.create_task(self._poll_loop(slug), name=f"poller:{slug}")
        return None

    async def remove_lane(self, lane: dict) -> None:
        """Stop delivery for a lane whose row was just deleted. Other lanes of
        the same bot keep receiving (the webhook moves to one of them); with
        none left, only a webhook that is ours is removed — never another
        system's."""
        self._stop_poller(lane["slug"])
        if db.bot_lanes(lane["bot_token"]):
            await self.sync_bot(lane["bot_token"], released=(lane["slug"],))
            return
        if settings.resolved_delivery_mode() == "off" or lane.get("receive_mode") == "send_only":
            return
        try:
            if await self.foreign_webhook(lane["bot_token"], [lane["slug"]]):
                return
            await telegram.delete_webhook(lane["bot_token"])
        except telegram.TelegramError as exc:
            LOG.warning("lane %s: deleteWebhook on remove failed: %s", lane["slug"], exc)

    def polling(self, slug: str) -> bool:
        """True when the lane's bot is being polled (by whichever of its lanes
        holds the poller)."""
        lane = db.get_lane(slug)
        slugs = [other["slug"] for other in db.bot_lanes(lane["bot_token"])] if lane else [slug]
        return any((task := self._pollers.get(s)) and not task.done() for s in slugs)

    async def stop_all(self) -> None:
        if self._autopilot_task:
            self._autopilot_task.cancel()
            self._autopilot_task = None
        for slug in list(self._pollers):
            self._stop_poller(slug)

    def _stop_poller(self, slug: str) -> None:
        task = self._pollers.pop(slug, None)
        if task:
            task.cancel()

    async def _autopilot_loop(self) -> None:
        LOG.info("autopilot background loop started (interval=30s)")
        while True:
            try:
                await autopilot_check_pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("autopilot loop error: %s", exc)
            await asyncio.sleep(30)

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
            await handle_incoming_update_commands(slug, upd)
        db.set_lane_state(slug, "next_offset", str(updates[-1]["update_id"] + 1))
        LOG.info("lane %s: ingested %d updates", slug, len(updates))


runtime = LaneRuntime()
