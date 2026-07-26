"""Per-lane delivery runtime.

Keeps each enabled lane wired to Telegram in the configured delivery mode:

- webhook: registers `{public_base_url}/{slug}/webhook` with the lane's secret;
  Telegram pushes updates, no background task needed.
- polling: runs one asyncio getUpdates long-poll task per lane (works without
  a public URL — laptops, NAT'd VPS, local testing).
- off: no Telegram delivery at all (tests).

`sync_lane` is called at startup for every lane and again whenever a lane is
created/updated/deleted from the admin API, so the runtime always reflects the
database.
"""
from __future__ import annotations

import asyncio
import logging

from . import db, telegram
from .config import settings

LOG = logging.getLogger("lanehub.runtime")


def ingest_update(lane_slug: str, upd: dict) -> None:
    """Persist one Telegram update (poller or webhook). No-op without a message."""
    msg = upd.get("message") or upd.get("channel_post")
    if not msg:
        return
    chat = msg.get("chat", {})
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
    )


class LaneRuntime:
    def __init__(self) -> None:
        self._pollers: dict[str, asyncio.Task] = {}

    def webhook_url(self, slug: str) -> str:
        return f"{settings.public_base_url}/{slug}/webhook"

    async def sync_all(self) -> None:
        for lane in db.list_lanes():
            await self.sync_lane(lane)

    async def sync_lane(self, lane: dict) -> str | None:
        """Bring one lane's delivery in line with its DB row.

        Returns a warning string when the Telegram side of the sync failed
        (the lane row itself is already saved) so callers can surface it."""
        slug = lane["slug"]
        self._stop_poller(slug)
        mode = settings.resolved_delivery_mode()
        if mode == "off":
            return None
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
        self._stop_poller(lane["slug"])
        if settings.resolved_delivery_mode() != "off":
            try:
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
        if not lane or not lane["enabled"]:
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
