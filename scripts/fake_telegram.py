"""A tiny fake Telegram Bot API server for local development and demos.

Lets you run the full hub (lanes, sending, webhooks/polling, feed) without any
real bot tokens. Point the hub at it:

    HUB_TELEGRAM_API=http://127.0.0.1:8081 HUB_ADMIN_PASSWORD=dev \
        uvicorn app.main:app --port 8090

    uvicorn scripts.fake_telegram:app --port 8081

Any token string is accepted; the bot username is derived from the token.
Simulate a human posting to the chat:

    curl -X POST http://127.0.0.1:8081/_push \
      -H 'Content-Type: application/json' \
      -d '{"token": "<lane bot token>", "from": "alice", "chat_id": -100500, "chat_title": "Team chat", "text": "hello"}'

In polling mode the hub picks the message up on the next getUpdates round; in
webhook mode the fake server POSTs it to the registered webhook URL.
"""
from __future__ import annotations

import itertools
import re
import time
from collections import defaultdict
from typing import Any

import httpx
from fastapi import FastAPI, Request

app = FastAPI(title="Fake Telegram Bot API")

_message_ids = itertools.count(1000)
_update_ids = itertools.count(1)
_pending: dict[str, list[dict]] = defaultdict(list)  # token -> updates for getUpdates
_webhooks: dict[str, dict] = {}  # token -> {url, secret}
sent_messages: list[dict] = []


def _bot_username(token: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9]", "", token.split(":", 1)[0]) or "fake"
    return f"{stem}_bot"


@app.post("/bot{token}/{method}")
async def bot_api(token: str, method: str, request: Request) -> dict:
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        payload = {}

    if method == "getMe":
        return {"ok": True, "result": {
            "id": abs(hash(token)) % 10**9, "is_bot": True,
            "first_name": "Fake Bot", "username": _bot_username(token),
        }}

    if method == "sendMessage":
        chat_id = payload.get("chat_id")
        try:
            chat_id_num = int(chat_id)
        except (TypeError, ValueError):
            chat_id_num = -100999
        result = {
            "message_id": next(_message_ids),
            "from": {"id": 1, "is_bot": True, "username": _bot_username(token)},
            "chat": {"id": chat_id_num, "title": "Fake chat", "type": "supergroup"},
            "date": int(time.time()),
            "text": payload.get("text", ""),
        }
        sent_messages.append({"token": token, **result})
        return {"ok": True, "result": result}

    if method == "setWebhook":
        _webhooks[token] = {"url": payload.get("url", ""), "secret": payload.get("secret_token", "")}
        return {"ok": True, "result": True}

    if method == "deleteWebhook":
        _webhooks.pop(token, None)
        return {"ok": True, "result": True}

    if method == "getWebhookInfo":
        wh = _webhooks.get(token, {})
        return {"ok": True, "result": {"url": wh.get("url", ""), "pending_update_count": 0}}

    if method == "getUpdates":
        offset = payload.get("offset")
        updates = _pending[token]
        if offset is not None:
            updates = [u for u in updates if u["update_id"] >= int(offset)]
        _pending[token] = []
        return {"ok": True, "result": updates}

    return {"ok": False, "description": f"fake server: method {method} not implemented"}


@app.post("/_push")
async def push(request: Request) -> dict:
    """Simulate an incoming human message for a bot (see module docstring)."""
    body = await request.json()
    token = body["token"]
    update = {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_message_ids),
            "from": {"id": 42, "is_bot": False, "username": body.get("from", "human")},
            "chat": {
                "id": body.get("chat_id", -100500),
                "title": body.get("chat_title", "Fake chat"),
                "type": "supergroup",
            },
            "date": int(time.time()),
            "text": body.get("text", ""),
        },
    }
    wh = _webhooks.get(token)
    if wh and wh.get("url"):
        async with httpx.AsyncClient() as client:
            await client.post(
                wh["url"], json=update,
                headers={"X-Telegram-Bot-Api-Secret-Token": wh.get("secret", "")},
                timeout=10,
            )
        return {"ok": True, "delivered": "webhook"}
    _pending[token].append(update)
    return {"ok": True, "delivered": "queued for getUpdates"}
