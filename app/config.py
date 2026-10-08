"""LaneHub configuration — everything comes from environment variables.

The settings object is a plain mutable dataclass instance so tests can swap
individual fields without re-importing modules.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "0.8.0"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    # SQLite file. Everything LaneHub knows lives here (lanes, keys, history).
    db_path: Path = field(default_factory=lambda: Path(_env("HUB_DB_PATH", "./data/hub.db")))

    # Password for the /admin web UI. Empty = admin UI is locked out entirely.
    admin_password: str = field(default_factory=lambda: _env("HUB_ADMIN_PASSWORD"))

    # Public HTTPS origin of this hub, e.g. https://hub.example.com.
    # Required for webhook mode (Telegram must be able to reach it).
    public_base_url: str = field(default_factory=lambda: _env("HUB_PUBLIC_BASE_URL").rstrip("/"))

    # Earlier public origins of THIS hub (comma-separated), e.g. after a domain
    # move. A webhook Telegram still sends to one of them counts as the hub's
    # own and is moved to the current address. Any other URL — even one with
    # the same /{slug}/webhook path — belongs to another system: a second hub
    # holding the same bot token must never be mistaken for this one.
    previous_base_urls: tuple[str, ...] = field(default_factory=lambda: tuple(
        u.strip().rstrip("/") for u in _env("HUB_PREVIOUS_BASE_URLS").split(",") if u.strip()
    ))

    # How often (seconds) the hub asks Telegram where each bot's updates go,
    # to notice another system taking the webhook. 0 = never.
    webhook_check_interval: float = field(
        default_factory=lambda: float(_env("HUB_WEBHOOK_CHECK_INTERVAL", "300"))
    )

    # An @mention older than this many hours does not wake the agent (the
    # watcher was off, the chat moved on); the owner is told instead. 0 = no limit.
    wake_max_age_hours: float = field(default_factory=lambda: float(_env("HUB_WAKE_MAX_AGE_HOURS", "24")))

    # webhook | polling | off. Default: webhook when public_base_url is set,
    # polling otherwise. "off" disables Telegram delivery (used in tests).
    delivery_mode: str = field(default_factory=lambda: _env("HUB_DELIVERY_MODE"))

    # Pause between getUpdates long-poll rounds (polling mode).
    poll_interval: float = field(default_factory=lambda: float(_env("HUB_POLL_INTERVAL", "2")))

    # Telegram Bot API origin — overridable for tests / local fake server.
    telegram_api: str = field(
        default_factory=lambda: _env("HUB_TELEGRAM_API", "https://api.telegram.org").rstrip("/")
    )

    def resolved_delivery_mode(self) -> str:
        if self.delivery_mode in ("webhook", "polling", "off"):
            return self.delivery_mode
        return "webhook" if self.public_base_url else "polling"


settings = Settings()
