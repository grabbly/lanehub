"""Invitation emails over SMTP.

SMTP can be configured two ways; the admin panel wins over environment:
1. Admin panel (Team → Email settings) — stored in the hub database.
2. HUB_SMTP_* environment variables — fallback for infra-as-code setups.
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from . import db
from .config import settings

LOG = logging.getLogger("lanehub.mailer")


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    from_addr: str
    tls: bool
    source: str  # "panel" | "env" | "none"


def smtp_config() -> SmtpConfig:
    host = (db.get_hub_state("smtp_host") or "").strip()
    if host:
        return SmtpConfig(
            host=host,
            port=int(db.get_hub_state("smtp_port") or 587),
            user=db.get_hub_state("smtp_user") or "",
            password=db.get_hub_state("smtp_password") or "",
            from_addr=db.get_hub_state("smtp_from") or "",
            tls=(db.get_hub_state("smtp_tls") or "1").lower() in ("1", "true", "yes"),
            source="panel",
        )
    if settings.smtp_host:
        return SmtpConfig(
            host=settings.smtp_host,
            port=settings.smtp_port,
            user=settings.smtp_user,
            password=settings.smtp_password,
            from_addr=settings.smtp_from,
            tls=settings.smtp_tls,
            source="env",
        )
    return SmtpConfig("", 587, "", "", "", True, "none")


def invite_text(email: str, password: str, login_url: str) -> str:
    return f"""Тебя пригласили в LaneHub — хаб командного Telegram-чата для AI-агентов.

Твой личный кабинет: {login_url}
Логин: {email}
Пароль: {password}

Зайди — там пошаговая инструкция на 2 минуты: создашь своего Telegram-бота
и получишь ключ доступа + готовые команды для своего AI-агента.

---

You've been invited to LaneHub — the team's Telegram hub for AI agents.

Your account: {login_url}
Login: {email}
Password: {password}

Log in for a 2-minute step-by-step guide: create your own Telegram bot and
get an access key + ready-made commands for your AI agent.
"""


def send_email(to_email: str, subject: str, body: str) -> bool:
    """Send one email with the effective SMTP config. Returns False when SMTP
    is not configured; raises on delivery errors so callers can surface them."""
    cfg = smtp_config()
    if not cfg.host:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr or cfg.user
    msg["To"] = to_email
    msg.set_content(body)
    with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as smtp:
        if cfg.tls:
            smtp.starttls()
        if cfg.user:
            smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)
    LOG.info("email sent to %s via %s (%s)", to_email, cfg.host, cfg.source)
    return True


def send_invite(email: str, password: str, login_url: str) -> bool:
    return send_email(
        email,
        "Приглашение в LaneHub / Your LaneHub invitation",
        invite_text(email, password, login_url),
    )
