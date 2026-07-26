"""Invitation emails over SMTP (optional — see HUB_SMTP_* settings)."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import settings

LOG = logging.getLogger("lanehub.mailer")


def invite_text(email: str, password: str, portal_url: str) -> str:
    return f"""Тебя пригласили в LaneHub — хаб командного Telegram-чата для AI-агентов.

Твой личный кабинет: {portal_url}
Логин: {email}
Пароль: {password}

Зайди — там пошаговая инструкция на 2 минуты: создашь своего Telegram-бота
и получишь ключ доступа + готовые команды для своего AI-агента.

---

You've been invited to LaneHub — the team's Telegram hub for AI agents.

Your portal: {portal_url}
Login: {email}
Password: {password}

Log in for a 2-minute step-by-step guide: create your own Telegram bot and
get an access key + ready-made commands for your AI agent.
"""


def send_invite(email: str, password: str, portal_url: str) -> bool:
    """Send the invitation email. Returns False when SMTP is not configured;
    raises on delivery errors so callers can surface them."""
    if not settings.smtp_host:
        return False
    msg = EmailMessage()
    msg["Subject"] = "Приглашение в LaneHub / Your LaneHub invitation"
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = email
    msg.set_content(invite_text(email, password, portal_url))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_tls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    LOG.info("invite sent to %s", email)
    return True
