"""Thin async client for the Telegram Bot API + message-shape helpers."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .config import settings

LOG = logging.getLogger("lanehub.telegram")


def mentions_bot(text: str, bot_username: str) -> bool:
    """True when `text` @-mentions `bot_username` as a whole token.

    Case-insensitive; `@foo_bot` does not match `@foo_bot2` (username chars are
    [A-Za-z0-9_], so we require a non-username char or end after the name)."""
    if not text or not bot_username:
        return False
    handle = re.escape(bot_username.lstrip("@"))
    return re.search(rf"@{handle}(?![A-Za-z0-9_])", text, re.IGNORECASE) is not None

# Telegram's hard limit is 4096 chars per message; we split a bit below it.
CHUNK_LIMIT = 4000


class TelegramError(Exception):
    def __init__(self, description: str, payload: dict | None = None):
        super().__init__(description)
        self.description = description
        self.payload = payload or {}


def _redact(exc: Exception, bot_token: str) -> str:
    """Exception text without the bot token — httpx errors may embed the URL,
    and these messages end up in logs and API error responses."""
    text = str(exc)
    return text.replace(bot_token, "<token>") if bot_token else text


async def tg_call(bot_token: str, method: str, payload: dict | None = None, timeout: float = 15) -> Any:
    """Call one Bot API method; return the `result` field or raise TelegramError."""
    url = f"{settings.telegram_api}/bot{bot_token}/{method}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload or {}, timeout=timeout)
        data = resp.json()
    except Exception as exc:  # network / JSON errors
        raise TelegramError(f"telegram unreachable: {_redact(exc, bot_token)}") from exc
    if not data.get("ok"):
        raise TelegramError(data.get("description", "telegram error"), data)
    return data.get("result")


async def get_me(bot_token: str) -> dict:
    return await tg_call(bot_token, "getMe")


async def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str | None = None,
    reply_to: int | None = None,
    disable_preview: bool = False,
) -> dict:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_to:
        # Still send if the replied-to message was deleted meanwhile.
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if disable_preview:
        payload["link_preview_options"] = {"is_disabled": True}
    return await tg_call(bot_token, "sendMessage", payload)


# sendPhoto takes jpeg/png/webp up to 10 MB (Telegram recompresses it);
# anything else goes out as a document (up to 50 MB via the Bot API).
PHOTO_MIMES = {"image/jpeg", "image/png", "image/webp"}
PHOTO_MAX = 10 * 1024 * 1024
UPLOAD_MAX = 50 * 1024 * 1024


async def tg_upload(bot_token: str, method: str, field: str, filename: str, content: bytes,
                    mime: str, data: dict, timeout: float = 120) -> Any:
    """Multipart variant of tg_call for sendPhoto / sendDocument."""
    url = f"{settings.telegram_api}/bot{bot_token}/{method}"
    form = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in data.items() if v is not None}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=form, files={field: (filename, content, mime)}, timeout=timeout)
        body = resp.json()
    except Exception as exc:
        raise TelegramError(f"telegram unreachable: {_redact(exc, bot_token)}") from exc
    if not body.get("ok"):
        raise TelegramError(body.get("description", "telegram error"), body)
    return body.get("result")


async def send_file(
    bot_token: str,
    chat_id: str,
    filename: str,
    content: bytes,
    mime: str,
    caption: str | None = None,
    parse_mode: str | None = None,
    reply_to: int | None = None,
    as_document: bool = False,
) -> dict:
    """Upload a file: small images via sendPhoto (inline preview), everything
    else — or anything when `as_document` — via sendDocument."""
    photo = not as_document and mime in PHOTO_MIMES and len(content) <= PHOTO_MAX
    method, field = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
    data: dict[str, Any] = {"chat_id": chat_id, "caption": caption or None, "parse_mode": parse_mode}
    if reply_to:
        data["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return await tg_upload(bot_token, method, field, filename, content, mime, data)


async def get_chat(bot_token: str, chat_id: str) -> dict:
    return await tg_call(bot_token, "getChat", {"chat_id": chat_id})


async def get_chat_member(bot_token: str, chat_id: str, user_id: int) -> dict:
    return await tg_call(bot_token, "getChatMember", {"chat_id": chat_id, "user_id": user_id})


def bot_id_from_token(bot_token: str) -> int | None:
    """A bot token is '<bot user id>:<secret>'."""
    head = (bot_token or "").split(":", 1)[0]
    return int(head) if head.isdigit() else None


def is_numeric_chat_id(value: str | None) -> bool:
    """True for a numeric chat id ('-100123…', '4242'); False for '@name' / ''."""
    v = (value or "").strip()
    return v.lstrip("-").isdigit()


async def resolve_chat(bot_token: str, raw: str) -> dict:
    """Validate a chat id (or @channelname) typed into the admin panel against
    Telegram and return getChat's answer — its `id` is the canonical one to store.

    Clients show supergroup ids in different forms: web.telegram.org drops the
    `-100` prefix (-4388659826 for -1004388659826). When the id as typed is
    unknown and could be such a stripped form, retry with `-100` in front.
    Raises TelegramError when neither works (the bot isn't in the chat, or the
    id is wrong)."""
    raw = raw.strip()
    try:
        return await get_chat(bot_token, raw)
    except TelegramError as exc:
        digits = raw.lstrip("-")
        if not digits.isdigit() or raw.startswith("-100"):
            raise
        try:
            return await get_chat(bot_token, f"-100{digits}")
        except TelegramError:
            raise exc from None


def _inline_kb(buttons: list[tuple[str, str]]) -> dict:
    """One row of inline buttons as (label, callback_data) pairs."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}


async def send_buttons(bot_token: str, chat_id: str, text: str, buttons: list[tuple[str, str]]) -> dict:
    return await tg_call(bot_token, "sendMessage",
                         {"chat_id": chat_id, "text": text, "reply_markup": _inline_kb(buttons)})


async def edit_message(bot_token: str, chat_id: str, message_id: int, text: str) -> dict:
    """Replace a message's text and drop its buttons (used after a decision)."""
    return await tg_call(bot_token, "editMessageText",
                         {"chat_id": chat_id, "message_id": message_id, "text": text})


async def answer_callback(bot_token: str, callback_id: str, text: str = "") -> None:
    try:
        await tg_call(bot_token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except TelegramError:
        pass  # answering is best-effort; the decision is already recorded


async def set_webhook(bot_token: str, url: str, secret: str) -> None:
    await tg_call(
        bot_token,
        "setWebhook",
        {"url": url, "secret_token": secret,
         "allowed_updates": ["message", "channel_post", "callback_query"]},
    )


async def delete_webhook(bot_token: str) -> None:
    await tg_call(bot_token, "deleteWebhook")


async def get_webhook_info(bot_token: str) -> dict:
    return await tg_call(bot_token, "getWebhookInfo")


async def get_updates(bot_token: str, offset: int | None, poll_timeout: int = 25) -> list[dict]:
    payload: dict[str, Any] = {
        "timeout": poll_timeout,
        "allowed_updates": ["message", "channel_post"],
    }
    if offset is not None:
        payload["offset"] = offset
    result = await tg_call(bot_token, "getUpdates", payload, timeout=poll_timeout + 10)
    return result or []


def extract_text(msg: dict) -> str:
    """Best-effort human-readable text for a Telegram message.

    Media messages have no `text`; the user note lives in `caption`. A
    `[kind: name]` marker makes attachments visible to pollers instead of a
    blank row. The file itself is described by extract_media() and can be
    fetched through GET /{lane}/file/{fileId}."""
    text = (msg.get("text") or msg.get("caption") or "").strip()
    marker = ""
    if "document" in msg:
        marker = f"[document: {msg['document'].get('file_name', 'file')}]"
    elif "photo" in msg:
        marker = "[photo]"
    elif "video" in msg:
        marker = "[video]"
    elif "video_note" in msg:
        marker = "[video note]"
    elif "voice" in msg:
        marker = "[voice]"
    elif "audio" in msg:
        marker = "[audio]"
    elif "sticker" in msg:
        marker = f"[sticker {msg['sticker'].get('emoji', '')}]".strip()
    if marker:
        return f"{marker} {text}".strip()
    return text


# Telegram message keys that carry a downloadable file, in extract_text's order.
_MEDIA_KINDS = ("document", "photo", "video", "video_note", "voice", "audio", "sticker", "animation")


def extract_media(msg: dict) -> dict | None:
    """Describe the downloadable attachment of a message, or None.

    Returns {kind, fileId, fileUniqueId, size, mime, name}. For photos the
    largest rendition is picked (Telegram sends several sizes). `fileId` is
    what GET /{lane}/file/{fileId} takes — it is only valid for the bot that
    received the update, which is why the hub proxies the download."""
    for kind in _MEDIA_KINDS:
        obj = msg.get(kind)
        if not obj:
            continue
        if kind == "photo":
            if not isinstance(obj, list):
                return None
            best = max(obj, key=lambda p: (p.get("file_size") or 0, p.get("width") or 0))
            return {
                "kind": "photo",
                "fileId": best.get("file_id"),
                "fileUniqueId": best.get("file_unique_id"),
                "size": best.get("file_size"),
                "mime": "image/jpeg",
                "name": f"{best.get('file_unique_id') or 'photo'}.jpg",
            }
        if not isinstance(obj, dict) or not obj.get("file_id"):
            return None
        mime = obj.get("mime_type") or {
            "voice": "audio/ogg", "video_note": "video/mp4",
            "sticker": "image/webp", "animation": "video/mp4",
        }.get(kind)
        return {
            "kind": kind,
            "fileId": obj["file_id"],
            "fileUniqueId": obj.get("file_unique_id"),
            "size": obj.get("file_size"),
            "mime": mime,
            "name": obj.get("file_name") or f"{obj.get('file_unique_id') or kind}{_ext_for(mime)}",
        }
    return None


def _ext_for(mime: str | None) -> str:
    return {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
        "video/mp4": ".mp4", "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "application/pdf": ".pdf",
    }.get(mime or "", "")


async def get_file(bot_token: str, file_id: str) -> dict:
    """Resolve a file_id to a short-lived Telegram file_path (getFile). Raises
    TelegramError for foreign/expired ids and for files over Bot API's 20 MB."""
    return await tg_call(bot_token, "getFile", {"file_id": file_id})


async def download_file(bot_token: str, file_path: str, timeout: float = 60) -> tuple[bytes, str]:
    """Download a file resolved by get_file(). Returns (bytes, content-type)."""
    url = f"{settings.telegram_api}/file/bot{bot_token}/{file_path}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=timeout)
    except Exception as exc:
        raise TelegramError(f"telegram unreachable: {_redact(exc, bot_token)}") from exc
    if resp.status_code != 200:
        raise TelegramError(f"telegram file download failed: HTTP {resp.status_code}")
    return resp.content, resp.headers.get("content-type", "application/octet-stream")


def extract_author(msg: dict) -> dict:
    """Structured author of a message: Telegram user id, @username and whether
    it's a bot. Channel posts / anonymous admins carry sender_chat instead."""
    sender = msg.get("from") or {}
    if sender:
        return {"from_id": sender.get("id"), "from_username": sender.get("username"),
                "from_is_bot": bool(sender.get("is_bot"))}
    sender_chat = msg.get("sender_chat") or {}
    return {"from_id": sender_chat.get("id"), "from_username": sender_chat.get("username"),
            "from_is_bot": False if sender_chat else None}


def extract_sender(msg: dict) -> str:
    """Sender label for groups (from) and channels (sender_chat / signature)."""
    sender = msg.get("from") or {}
    if sender:
        return sender.get("username") or sender.get("first_name") or str(sender.get("id", ""))
    sender_chat = msg.get("sender_chat") or {}
    if sender_chat:
        return sender_chat.get("title") or sender_chat.get("username") or str(sender_chat.get("id", ""))
    return msg.get("author_signature") or ""


def chunk_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split long text into <=limit chunks, preferring newline/space boundaries."""
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 1, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 1, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n ")
    return chunks
