"""Messenger lanes: a Facebook Messenger group chat, read by a browser extension.

Meta gives bots no way into Messenger group chats, so a Messenger lane has no
bot API behind it. A browser extension, signed in to Messenger as the bot's
account, reads the group page and pushes what it sees to the hub
(POST /{lane}/messenger/ingest, voice clips via /messenger/voice, photos via
/messenger/photo). The hub stores those rows like Telegram ones, so
/messages, /feed, /wake and the watcher work unchanged.

Writing goes the other way round and stays manual: /send on a Messenger lane
queues a draft, the extension types it into the chat's message box, and a
person presses Send. When the message then shows up in the chat, the draft
is marked sent.

A Messenger lane keeps a placeholder bot token (`messenger:<slug>`), unique
per lane and never valid at Telegram, so code keyed by bot token never mixes
it with a real bot; runtime skips it for webhooks and polling.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import db
from .config import settings

TOKEN_PREFIX = "messenger:"
FILE_PREFIX = "mx_"
VOICE_MAX = 25 * 1024 * 1024
PHOTO_MAX = 25 * 1024 * 1024
PHOTOS_PER_MESSAGE = 30

# Image types the hub keeps, told apart by their first bytes (the
# extension's own content type is not trusted).
_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)

# A Messenger thread URL (https://www.messenger.com/t/<id>, also /e2ee/t/<id>)
# or the bare numeric thread id.
_THREAD_RE = re.compile(r"(?:/t/)?(\d{5,20})/?(?:[?#].*)?$")


def is_messenger(lane: dict | None) -> bool:
    return bool(lane) and (lane.get("transport") or "telegram") == "messenger"


def is_messenger_token(bot_token: str | None) -> bool:
    return (bot_token or "").startswith(TOKEN_PREFIX)


def placeholder_token(slug: str) -> str:
    return f"{TOKEN_PREFIX}{slug}"


def thread_id(raw: str) -> str:
    """The numeric thread id from a pasted Messenger URL or id; '' if none."""
    m = _THREAD_RE.search((raw or "").strip())
    return m.group(1) if m else ""


def mentions(text: str, name: str) -> bool:
    """True when `text` names the bot account. Messenger shows a picked
    mention as the plain name, without "@"; typed "@Name" counts too."""
    if not text or not name.strip():
        return False
    return re.search(rf"(?<![\w@])@?{re.escape(name.strip())}(?!\w)", text, re.IGNORECASE) is not None


def message_number(ext_id: str) -> int:
    """A stable positive integer (53 bits, safe for JSON clients) for the
    extension's message id — the hub's message_id column is numeric."""
    digest = hashlib.sha256(ext_id.encode()).digest()
    return (int.from_bytes(digest[:8], "big") >> 11) or 1


def _next_update_id(lane_slug: str) -> int:
    """Per-lane counter for incoming rows: increasing, far below the
    OUTGOING_BASE namespace, like Telegram's own update_ids."""
    n = int(db.get_lane_state(lane_slug, "mx_update_id") or 0) + 1
    db.set_lane_state(lane_slug, "mx_update_id", str(n))
    return n


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _match_draft(lane_slug: str, text: str) -> None:
    """Mark the oldest open draft with this text as sent."""
    said = _norm(text)
    if not said:
        return
    for draft in db.list_drafts(lane_slug):
        if _norm(draft["text"]) == said:
            db.set_draft_status(lane_slug, draft["id"], "sent")
            return


def ingest(lane: dict, chat_title: str, messages: list[dict], backfill: bool = False) -> list[dict]:
    """Store messages the extension read from the lane's bound chat.

    Each message: {id, from, text, date?, outgoing?, replyTo?, kind?, photos?}.
    `id` is the extension's stable id for the message; sending the same id
    again refreshes the row (text edits) without making a new one, and keeps
    media and date already stored. `outgoing` marks the bot account's own
    messages.

    `photos`: [{fileId, width?, height?}] of images already uploaded through
    /messenger/photo (the photos section below says how they map onto rows).

    `backfill`: history already on the page when it was opened (or scrolled
    up to), not something just said. It is stored as history: an old
    @mention in it never wakes the watcher."""
    chat_id = int(lane["default_chat_id"])
    now = int(time.time())
    out = []
    for m in messages:
        message_id = message_number(m["id"])
        old = db.existing_message(lane["slug"], chat_id, message_id)
        text = m.get("text") or ""
        if m.get("kind") == "voice" and not text.startswith("[voice]"):
            text = f"[voice] {text}".strip()
        photos = _known_photos(m.get("photos") or [])
        media = json.loads(old["media"]) if old and old.get("media") else None
        if photos and not old:
            media = _photo_media(photos[0], m["id"], 1, len(photos), int(m.get("date") or now))
        if (media or {}).get("kind") == "photo":
            text = _photo_text(text, 1, media.get("count") or 1)
        elif m.get("kind") == "photo" or photos:
            text = _photo_text(text, 1, 1)
        outgoing = bool(m.get("outgoing"))
        row = db.store_message(
            lane_slug=lane["slug"],
            update_id=old["update_id"] if old else _next_update_id(lane["slug"]),
            message_id=message_id,
            chat_id=chat_id,
            chat_title=chat_title or (old or {}).get("chat_title") or lane.get("title") or "",
            from_user=m.get("from") or "",
            text=text,
            date=(old or {}).get("date") or int(m.get("date") or now),
            is_outgoing=outgoing,
            media=media,
            from_username=m.get("from") or None,
            from_is_bot=outgoing,
            reply_to_username=m.get("replyTo") or None,
            history=bool(old["history"]) if old else backfill,
        )
        if outgoing and not old:
            _match_draft(lane["slug"], text)
        out.append({"id": m["id"], "new": old is None, **row})
        if photos:
            first = 1 if (media or {}).get("kind") == "photo" else 0
            out.extend(_photo_rows(lane, m, photos[first:], first, len(photos), chat_title, backfill))
    return out


# --- photos ------------------------------------------------------------------
#
# One message can hold several photos, while a feed row carries one `media`
# object (that is what tg-fetch.sh and tg-file.sh read). So a photo message
# becomes several rows, the way Telegram delivers an album: the message itself
# with the first photo ("[photo 1/3] caption"), then one row per further photo
# ("[photo 2/3]", "[photo 3/3]"), each with its own media and fileId. The
# extra rows have their own stable ids (`<message id>#photo<n>`), so reading
# the message again never duplicates them.
#
# A row already in the feed is never given media afterwards: an agent that
# fetched it would not see the change. Photos that arrive after their message
# go into extra rows only, starting with photo 1.


def _photo_text(text: str, n: int, count: int) -> str:
    text = re.sub(r"^\[photo(?: \d+/\d+)?\]\s*", "", text or "")
    marker = "[photo]" if count <= 1 else f"[photo {n}/{count}]"
    return f"{marker} {text}".strip()


def _sniff_image(head: bytes) -> tuple[str, str] | None:
    for magic, mime, ext in _IMAGE_MAGIC:
        if head.startswith(magic):
            return mime, ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"avif"):
        return ("image/avif", ".avif") if head[8:12] == b"avif" else ("image/heic", ".heic")
    return None


def image_type(content: bytes) -> tuple[str, str] | None:
    """(mime, extension) of an image upload, None when it is not an image."""
    return _sniff_image(content[:16])


def _known_photos(photos: list[dict]) -> list[dict]:
    """The photos whose files are on the hub, with their size and type."""
    out = []
    for p in photos[:PHOTOS_PER_MESSAGE]:
        fid = p.get("fileId") or ""
        data = read_file(fid)
        kind = image_type(data) if data else None
        if not kind:
            continue
        out.append({**p, "fileId": fid, "size": len(data), "mime": kind[0], "ext": kind[1]})
    return out


def _photo_media(photo: dict, ext_id: str, n: int, count: int, date: int) -> dict:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(date))
    short = hashlib.sha256(ext_id.encode()).hexdigest()[:8]
    media = {
        "kind": "photo", "fileId": photo["fileId"], "fileUniqueId": photo["fileId"],
        "size": photo["size"], "mime": photo["mime"],
        "name": f"messenger-{stamp}-{short}-{n}{photo['ext']}",
        "index": n, "count": count,
    }
    for key in ("width", "height"):
        if isinstance(photo.get(key), int) and photo[key] > 0:
            media[key] = photo[key]
    return media


def _photo_rows(lane: dict, m: dict, photos: list[dict], skipped: int, count: int,
                chat_title: str, backfill: bool) -> list[dict]:
    """Extra rows for photos that are not on the message's own row."""
    chat_id = int(lane["default_chat_id"])
    parent = db.existing_message(lane["slug"], chat_id, message_number(m["id"])) or {}
    out = []
    for i, photo in enumerate(photos, start=skipped + 1):
        ext_id = f"{m['id']}#photo{i}"
        message_id = message_number(ext_id)
        if db.existing_message(lane["slug"], chat_id, message_id):
            continue
        date = parent.get("date") or int(time.time())
        outgoing = bool(m.get("outgoing"))
        row = db.store_message(
            lane_slug=lane["slug"],
            update_id=_next_update_id(lane["slug"]),
            message_id=message_id,
            chat_id=chat_id,
            chat_title=chat_title or parent.get("chat_title") or lane.get("title") or "",
            from_user=m.get("from") or "",
            text=_photo_text("", i, count),
            date=date,
            is_outgoing=outgoing,
            media=_photo_media(photo, m["id"], i, count, date),
            from_username=m.get("from") or None,
            from_is_bot=outgoing,
            history=bool(parent["history"]) if parent else backfill,
        )
        out.append({"id": ext_id, "new": True, **row})
    return out


def files_dir() -> Path:
    return settings.db_path.parent / "messenger-files"


def save_file(content: bytes) -> str:
    """Keep an uploaded clip next to the database; returns its file id."""
    fid = FILE_PREFIX + hashlib.sha256(content).hexdigest()[:32]
    path = files_dir() / fid
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return fid


def read_file(fid: str) -> bytes | None:
    if not re.fullmatch(rf"{FILE_PREFIX}[0-9a-f]{{32}}", fid or ""):
        return None
    path = files_dir() / fid
    return path.read_bytes() if path.exists() else None


def attach_voice(lane: dict, ext_id: str, content: bytes, mime: str, duration: int | None) -> dict | None:
    """Attach an uploaded voice clip to a stored message and queue it for
    transcription. None when the message hasn't been ingested yet."""
    chat_id = int(lane["default_chat_id"])
    old = db.existing_message(lane["slug"], chat_id, message_number(ext_id))
    if not old:
        return None
    fid = save_file(content)
    ext = {"audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/webm": ".webm",
           "video/mp4": ".mp4"}.get(mime, "")
    media = {"kind": "voice", "fileId": fid, "fileUniqueId": fid, "size": len(content), "mime": mime,
             "name": f"{fid}{ext}"}
    if duration is not None:
        media["duration"] = int(duration)
    db.store_message(
        lane_slug=lane["slug"], update_id=old["update_id"], message_id=old["message_id"], chat_id=chat_id,
        chat_title=old["chat_title"], from_user=old["from_user"], text=old["text"], date=old["date"],
        is_outgoing=bool(old["is_outgoing"]), media=media, from_username=old["from_username"],
        from_is_bot=None if old["from_is_bot"] is None else bool(old["from_is_bot"]),
        reply_to_username=old["reply_to_username"], history=bool(old["history"]),
    )
    if settings.stt_enabled:
        too_long = duration is not None and duration > settings.stt_max_seconds
        db.enqueue_transcript(
            file_unique_id=fid, lane_slug=lane["slug"], file_id=fid, kind="voice", duration=duration,
            status="skipped" if too_long else "pending",
            error=f"media duration ({duration}s) exceeds maximum ({settings.stt_max_seconds}s)" if too_long else None,
        )
    return media
