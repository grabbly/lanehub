import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

ADMIN_PASSWORD = "test-pass"


class FakeTG:
    """In-memory stand-in for telegram.tg_call — records every Bot API call."""

    def __init__(self):
        self.calls = []
        self.next_message_id = 100

    async def __call__(self, bot_token, method, payload=None, timeout=15):
        payload = payload or {}
        self.calls.append((bot_token, method, payload))
        if method == "getMe":
            return {"id": 1, "is_bot": True, "first_name": "Test Bot", "username": "test_bot"}
        if method == "sendMessage":
            self.next_message_id += 1
            try:
                chat_id = int(payload.get("chat_id"))
            except (TypeError, ValueError):
                chat_id = -100999
            return {
                "message_id": self.next_message_id,
                "from": {"id": 1, "is_bot": True, "username": "test_bot"},
                "chat": {"id": chat_id, "title": "Test chat", "type": "supergroup"},
                "date": 1_700_000_000,
                "text": payload.get("text", ""),
            }
        if method in ("setWebhook", "deleteWebhook"):
            return True
        if method == "getWebhookInfo":
            return {"url": "", "pending_update_count": 0}
        if method == "getUpdates":
            return []
        if method == "getFile":
            fid = payload["file_id"]
            if fid.startswith("bad"):
                from app.telegram import TelegramError
                raise TelegramError("Bad Request: invalid file_id")
            if fid.startswith("big"):
                from app.telegram import TelegramError
                raise TelegramError("Bad Request: file is too big")
            return {"file_id": fid, "file_unique_id": fid[:6], "file_size": 3, "file_path": f"photos/{fid}.jpg"}
        raise AssertionError(f"unexpected Bot API method {method}")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "db_path", tmp_path / "hub.db")
    monkeypatch.setattr(settings, "admin_password", ADMIN_PASSWORD)
    monkeypatch.setattr(settings, "delivery_mode", "off")
    monkeypatch.setattr(settings, "public_base_url", "")

    fake = FakeTG()
    monkeypatch.setattr("app.telegram.tg_call", fake)

    async def fake_download(bot_token, file_path, timeout=60):
        fake.calls.append((bot_token, "download", {"file_path": file_path}))
        return b"\xff\xd8\xff", "image/jpeg"

    monkeypatch.setattr("app.telegram.download_file", fake_download)

    from app.main import create_app

    with TestClient(create_app()) as c:
        c.fake_tg = fake
        yield c


def login(client):
    """Admin sign-in via the unified endpoint: blank email + admin password."""
    resp = client.post("/api/login", json={"email": "", "password": ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text


def member_login(client, email, password):
    """Member sign-in via the unified endpoint."""
    return client.post("/api/login", json={"email": email, "password": password})


def make_lane(client, slug="backend", chat_id="-100500", **extra):
    resp = client.post(
        "/admin/api/lanes",
        json={"slug": slug, "botToken": f"{slug}-token:abc", "defaultChatId": chat_id, **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
