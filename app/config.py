"""LaneHub configuration — everything comes from environment variables.

The settings object is a plain mutable dataclass instance so tests can swap
individual fields without re-importing modules.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "0.3.0"


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

    # webhook | polling | off. Default: webhook when public_base_url is set,
    # polling otherwise. "off" disables Telegram delivery (used in tests).
    delivery_mode: str = field(default_factory=lambda: _env("HUB_DELIVERY_MODE"))

    # Pause between getUpdates long-poll rounds (polling mode).
    poll_interval: float = field(default_factory=lambda: float(_env("HUB_POLL_INTERVAL", "2")))

    # Telegram Bot API origin — overridable for tests / local fake server.
    telegram_api: str = field(
        default_factory=lambda: _env("HUB_TELEGRAM_API", "https://api.telegram.org").rstrip("/")
    )

    # SMTP for invitation emails — optional. Without it, the invite dialog
    # still generates credentials + a copy-paste invite text.
    smtp_host: str = field(default_factory=lambda: _env("HUB_SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("HUB_SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: _env("HUB_SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("HUB_SMTP_PASSWORD"))
    smtp_from: str = field(default_factory=lambda: _env("HUB_SMTP_FROM"))
    smtp_tls: bool = field(default_factory=lambda: _env("HUB_SMTP_TLS", "1").lower() in ("1", "true", "yes"))

    def resolved_delivery_mode(self) -> str:
        if self.delivery_mode in ("webhook", "polling", "off"):
            return self.delivery_mode
        return "webhook" if self.public_base_url else "polling"


settings = Settings()
