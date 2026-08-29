"""Superadmin API for the web UI.

Login/logout live in routes_auth (POST /api/login sets the shared `hub_session`
cookie — an HMAC over an expiry + subject, secret persisted in the DB). This
module owns the token machinery (sign_token/token_subject/require_admin) plus
every /admin/api/* endpoint, all of which require an admin-subject cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets as pysecrets
import time

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel, Field

from . import db, mailer, telegram
from .config import settings
from .routes_bridge import perform_send
from .runtime import runtime

router = APIRouter(prefix="/admin/api")

SESSION_COOKIE = "hub_session"
SESSION_TTL = 7 * 24 * 3600
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def sign_token(subject: str, expiry: int) -> str:
    """Signed session token carrying a subject: 'admin' or a member email."""
    mac = hmac.new(db.session_secret().encode(), f"{expiry}:{subject}".encode(), hashlib.sha256)
    return f"{expiry}.{mac.hexdigest()}.{subject}"


def token_subject(token: str | None) -> str | None:
    if not token:
        return None
    parts = token.split(".", 2)
    if len(parts) != 3:
        return None
    expiry_raw, mac, subject = parts
    try:
        expiry = int(expiry_raw)
    except ValueError:
        return None
    if expiry <= time.time():
        return None
    expected = hmac.new(
        db.session_secret().encode(), f"{expiry}:{subject}".encode(), hashlib.sha256
    ).hexdigest()
    return subject if pysecrets.compare_digest(mac, expected) else None


def _check_session(token: str | None) -> bool:
    return token_subject(token) == "admin"


def require_admin(hub_session: str | None = Cookie(default=None)) -> None:
    if not _check_session(hub_session):
        raise HTTPException(status_code=401, detail="admin login required")


def _lane_view(lane: dict) -> dict:
    mode = settings.resolved_delivery_mode()
    return {
        "slug": lane["slug"],
        "title": lane["title"],
        "botUsername": lane["bot_username"],
        "apiKey": lane["api_key"],
        "defaultChatId": lane["default_chat_id"],
        "enabled": bool(lane["enabled"]),
        "createdAt": lane["created_at"],
        "deliveryMode": mode,
        "webhookUrl": runtime.webhook_url(lane["slug"]) if mode == "webhook" else None,
        "polling": runtime.polling(lane["slug"]),
        "storedMessages": db.count_messages(lane["slug"]),
        "seenChats": db.seen_chats(lane["slug"]),
    }


class LaneCreate(BaseModel):
    slug: str = ""  # optional — derived from the bot's username when empty
    title: str = ""
    bot_token: str = Field(alias="botToken")
    default_chat_id: str = Field(default="", alias="defaultChatId")

    model_config = {"populate_by_name": True}


def derive_slug(bot_username: str) -> str:
    """Auto-slug from a bot username: '@denis_team_bot' → 'denis_team',
    made unique against existing lanes and reserved names."""
    base = re.sub(r"[^a-z0-9_-]", "", (bot_username or "").lower())
    for suffix in ("_bot", "-bot", "bot"):
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
            break
    base = base.strip("_-")[:28] or "lane"
    if not re.match(r"^[a-z0-9]", base):
        base = f"x{base}"
    candidate, i = base, 2
    while candidate in db.RESERVED_SLUGS or db.get_lane(candidate):
        candidate, i = f"{base}-{i}", i + 1
    return candidate


class LaneUpdate(BaseModel):
    title: str | None = None
    bot_token: str | None = Field(default=None, alias="botToken")
    default_chat_id: str | None = Field(default=None, alias="defaultChatId")
    enabled: bool | None = None

    model_config = {"populate_by_name": True}


class AdminSend(BaseModel):
    text: str = Field(min_length=1, max_length=64000)
    chat_id: str | None = Field(default=None, alias="chatId")

    model_config = {"populate_by_name": True}


@router.get("/lanes")
async def lanes_list(hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    return {"lanes": [_lane_view(lane) for lane in db.list_lanes()]}


@router.post("/lanes", status_code=201)
async def lanes_create(req: LaneCreate, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    slug = req.slug.strip().lower()
    if slug:
        if not SLUG_RE.match(slug):
            raise HTTPException(status_code=422, detail="slug must match [a-z0-9][a-z0-9_-]{0,31}")
        if slug in db.RESERVED_SLUGS:
            raise HTTPException(status_code=422, detail=f"slug '{slug}' is reserved")
        if db.get_lane(slug):
            raise HTTPException(status_code=409, detail="lane already exists")
    token = req.bot_token.strip()
    try:
        me = await telegram.get_me(token)
    except telegram.TelegramError as exc:
        raise HTTPException(status_code=422, detail=f"bot token rejected by Telegram: {exc}")
    if not slug:
        slug = derive_slug(me.get("username", ""))
    lane_dict = db.create_lane(
        slug=slug,
        title=req.title.strip() or me.get("first_name", ""),
        bot_token=token,
        bot_username=me.get("username", ""),
        # empty → inherit the hub-wide project chat, so freshly onboarded
        # members' lanes post to the team chat with zero configuration
        default_chat_id=req.default_chat_id.strip() or db.get_hub_state("project_chat_id") or "",
    )
    warning = await runtime.sync_lane(lane_dict)
    view = _lane_view(lane_dict)
    if warning:
        view["warning"] = warning
    return view


@router.patch("/lanes/{slug}")
async def lanes_update(slug: str, req: LaneUpdate, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    lane = db.get_lane(slug)
    if not lane:
        raise HTTPException(status_code=404, detail="unknown lane")
    fields: dict = {}
    if req.title is not None:
        fields["title"] = req.title.strip()
    if req.default_chat_id is not None:
        fields["default_chat_id"] = req.default_chat_id.strip()
    if req.enabled is not None:
        fields["enabled"] = int(req.enabled)
    if req.bot_token is not None:
        token = req.bot_token.strip()
        try:
            me = await telegram.get_me(token)
        except telegram.TelegramError as exc:
            raise HTTPException(status_code=422, detail=f"bot token rejected by Telegram: {exc}")
        fields["bot_token"] = token
        fields["bot_username"] = me.get("username", "")
    lane = db.update_lane(slug, fields)
    assert lane is not None
    warning = await runtime.sync_lane(lane)
    view = _lane_view(lane)
    if warning:
        view["warning"] = warning
    return view


@router.post("/lanes/{slug}/rotate-key")
async def lanes_rotate_key(slug: str, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    if not db.get_lane(slug):
        raise HTTPException(status_code=404, detail="unknown lane")
    return {"apiKey": db.rotate_lane_key(slug)}


@router.delete("/lanes/{slug}")
async def lanes_delete(slug: str, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    lane = db.get_lane(slug)
    if not lane:
        raise HTTPException(status_code=404, detail="unknown lane")
    await runtime.remove_lane(lane)
    db.delete_lane(slug)
    return {"ok": True, "note": "lane removed; message history kept"}


@router.get("/lanes/{slug}/webhook-info")
async def lanes_webhook_info(slug: str, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    lane = db.get_lane(slug)
    if not lane:
        raise HTTPException(status_code=404, detail="unknown lane")
    try:
        return await telegram.get_webhook_info(lane["bot_token"])
    except telegram.TelegramError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/lanes/{slug}/send")
async def lanes_send(slug: str, req: AdminSend, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    lane = db.get_lane(slug)
    if not lane:
        raise HTTPException(status_code=404, detail="unknown lane")
    return await perform_send(lane, req.text, req.chat_id)


# --- team: project chat + member invitations -------------------------------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def login_url() -> str:
    """The single sign-in entrance members log into."""
    return settings.public_base_url or "/"


class SettingsUpdate(BaseModel):
    project_chat_id: str | None = Field(default=None, alias="projectChatId")
    smtp_host: str | None = Field(default=None, alias="smtpHost")
    smtp_port: int | None = Field(default=None, alias="smtpPort")
    smtp_user: str | None = Field(default=None, alias="smtpUser")
    smtp_password: str | None = Field(default=None, alias="smtpPassword")
    smtp_from: str | None = Field(default=None, alias="smtpFrom")
    smtp_tls: bool | None = Field(default=None, alias="smtpTls")

    model_config = {"populate_by_name": True}


class MemberInvite(BaseModel):
    email: str
    name: str = ""


class TestEmail(BaseModel):
    to: str


@router.get("/lanes/{slug}/logs")
async def lanes_logs(slug: str, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    if not db.get_lane(slug):
        raise HTTPException(status_code=404, detail="unknown lane")
    return {"slug": slug, "logs": db.query_lane_logs(slug)}


@router.get("/settings")
async def settings_get(hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    cfg = mailer.smtp_config()
    return {
        "projectChatId": db.get_hub_state("project_chat_id") or "",
        "loginUrl": login_url(),
        "smtpConfigured": cfg.source != "none",
        # password itself is never echoed back — only whether one is stored
        "smtp": {
            "host": cfg.host,
            "port": cfg.port,
            "user": cfg.user,
            "from": cfg.from_addr,
            "tls": cfg.tls,
            "passwordSet": bool(cfg.password),
            "source": cfg.source,
        },
    }


@router.patch("/settings")
async def settings_update(req: SettingsUpdate, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    if req.project_chat_id is not None:
        db.set_hub_state("project_chat_id", req.project_chat_id.strip())
    # SMTP fields: only provided keys are written; password omitted = unchanged,
    # empty host = drop panel config (fall back to env / none)
    if req.smtp_host is not None:
        db.set_hub_state("smtp_host", req.smtp_host.strip())
    if req.smtp_port is not None:
        db.set_hub_state("smtp_port", str(req.smtp_port))
    if req.smtp_user is not None:
        db.set_hub_state("smtp_user", req.smtp_user.strip())
    if req.smtp_password is not None:
        db.set_hub_state("smtp_password", req.smtp_password)
    if req.smtp_from is not None:
        db.set_hub_state("smtp_from", req.smtp_from.strip())
    if req.smtp_tls is not None:
        db.set_hub_state("smtp_tls", "1" if req.smtp_tls else "0")
    return await settings_get(hub_session)


@router.post("/settings/test-email")
async def settings_test_email(req: TestEmail, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    to = req.to.strip().lower()
    if not EMAIL_RE.match(to):
        raise HTTPException(status_code=422, detail="invalid email")
    try:
        sent = mailer.send_email(
            to,
            "LaneHub test email",
            "SMTP settings work — this is a test email from your LaneHub admin panel.\n\n"
            "SMTP настроен верно — это проверочное письмо из админ-панели LaneHub.",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"send failed: {exc}")
    if not sent:
        raise HTTPException(status_code=400, detail="SMTP is not configured")
    return {"ok": True}


def _member_view(m: dict) -> dict:
    return {
        "email": m["email"],
        "name": m["name"],
        "laneSlug": m["lane_slug"] or None,
        "createdAt": m["created_at"],
        "lastLogin": m["last_login"] or None,
    }


def _invite_payload(email: str, name: str) -> dict:
    """(Re)issue credentials for a member and try to email them."""
    password = pysecrets.token_urlsafe(9)
    if db.get_member(email):
        db.update_member(email, {"password_hash": db.hash_password(password)})
    else:
        db.create_member(email, name, db.hash_password(password))
    text = mailer.invite_text(email, password, login_url())
    email_sent, email_error = False, None
    try:
        email_sent = mailer.send_invite(email, password, login_url())
    except Exception as exc:
        email_error = str(exc)
    return {
        "email": email,
        "password": password,
        "loginUrl": login_url(),
        "inviteText": text,
        "emailSent": email_sent,
        "emailError": email_error,
    }


@router.get("/members")
async def members_list(hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    return {"members": [_member_view(m) for m in db.list_members()]}


@router.post("/members", status_code=201)
async def members_invite(req: MemberInvite, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    email = req.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="invalid email")
    if db.get_member(email):
        raise HTTPException(status_code=409, detail="member already invited (use reset-password to re-issue)")
    return _invite_payload(email, req.name.strip())


@router.post("/members/{email}/reset-password")
async def members_reset_password(email: str, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    member = db.get_member(email.strip().lower())
    if not member:
        raise HTTPException(status_code=404, detail="unknown member")
    return _invite_payload(member["email"], member["name"])


@router.delete("/members/{email}")
async def members_delete(email: str, hub_session: str | None = Cookie(default=None)) -> dict:
    require_admin(hub_session)
    member = db.get_member(email.strip().lower())
    if not member:
        raise HTTPException(status_code=404, detail="unknown member")
    db.delete_member(member["email"])
    return {"ok": True, "note": "member removed; their lane (if any) is untouched — manage it in Lanes"}


@router.get("/feed")
async def admin_feed(
    limit: int = 100,
    order: str = "desc",
    sinceDate: int = 0,
    chatId: int | None = None,
    hub_session: str | None = Cookie(default=None),
) -> dict:
    require_admin(hub_session)
    limit = max(1, min(limit, 500))
    if order not in ("asc", "desc"):
        order = "desc"
    rows = db.query_feed(sinceDate, limit, order, chatId)
    return {"messages": rows, "count": len(rows), "chats": db.seen_chats()}
