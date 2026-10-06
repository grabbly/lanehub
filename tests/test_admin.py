"""Admin API: auth, lane management, feed, send."""
from conftest import ADMIN_PASSWORD, login, make_lane


def test_admin_requires_login(client):
    assert client.get("/admin/api/lanes").status_code == 401
    assert client.post("/admin/api/lanes", json={"slug": "x", "botToken": "t"}).status_code == 401


def test_wrong_password(client):
    resp = client.post("/api/login", json={"email": "", "password": "nope"})
    assert resp.status_code == 401


def test_session_reflects_auth(client):
    assert client.get("/api/session").json()["authenticated"] is False
    login(client)
    st = client.get("/api/session").json()
    assert st["authenticated"] is True
    assert st["adminPasswordSet"] is True


def test_lane_validation(client):
    login(client)
    bad = client.post("/admin/api/lanes", json={"slug": "Bad Slug!", "botToken": "t:1"})
    assert bad.status_code == 422
    no_token = client.post("/admin/api/lanes", json={"slug": "x"})
    assert no_token.status_code == 422
    reserved = client.post("/admin/api/lanes", json={"slug": "admin", "botToken": "t:1"})
    assert reserved.status_code == 422
    make_lane(client, slug="dup")
    again = client.post("/admin/api/lanes", json={"slug": "dup", "botToken": "t:1"})
    assert again.status_code == 409


def test_auto_slug_from_bot_username(client):
    login(client)
    # no slug given → derived from the bot username ("test_bot" → "test")
    r1 = client.post("/admin/api/lanes", json={"botToken": "a:1"})
    assert r1.status_code == 201, r1.text
    assert r1.json()["slug"] == "test"
    # same bot username again → uniquified
    r2 = client.post("/admin/api/lanes", json={"botToken": "b:2"})
    assert r2.status_code == 201
    assert r2.json()["slug"] == "test-2"
    # explicit slug still wins
    r3 = client.post("/admin/api/lanes", json={"slug": "custom", "botToken": "c:3"})
    assert r3.json()["slug"] == "custom"


def test_lane_lifecycle(client):
    login(client)
    lane = make_lane(client, slug="qa", title="QA agent")
    assert lane["botUsername"] == "test_bot"
    assert lane["title"] == "QA agent"
    assert lane["apiKey"]

    lanes = client.get("/admin/api/lanes").json()["lanes"]
    assert [l["slug"] for l in lanes] == ["qa"]

    upd = client.patch("/admin/api/lanes/qa", json={"defaultChatId": "-100777"}).json()
    assert upd["defaultChatId"] == "-100777"

    resp = client.delete("/admin/api/lanes/qa")
    assert resp.status_code == 200
    assert client.get("/admin/api/lanes").json()["lanes"] == []
    # bridge API for the removed lane is gone too
    assert client.get("/qa/messages", headers={"X-Bridge-Token": lane["apiKey"]}).status_code == 404


def test_admin_send_and_feed(client):
    login(client)
    make_lane(client, slug="ops")
    resp = client.post("/admin/api/lanes/ops/send", json={"text": "status ping"})
    assert resp.status_code == 200
    feed = client.get("/admin/api/feed").json()
    assert feed["messages"][0]["text"] == "status ping"
    assert feed["messages"][0]["lane"] == "ops"
    assert feed["chats"][0]["chatId"] == -100500


def test_logout(client):
    login(client)
    assert client.get("/admin/api/lanes").status_code == 200
    client.post("/api/logout")
    assert client.get("/admin/api/lanes").status_code == 401


def test_history_survives_lane_delete(client):
    login(client)
    make_lane(client, slug="temp")
    client.post("/admin/api/lanes/temp/send", json={"text": "keep me"})
    client.delete("/admin/api/lanes/temp")
    feed = client.get("/admin/api/feed").json()
    assert any(m["text"] == "keep me" for m in feed["messages"])


# --- 0.5.4: never take over a bot whose updates belong to another system ---

PORTAL = "https://portal.example.com/api/telegram/webhook"


def _webhook_mode(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "delivery_mode", "webhook")
    monkeypatch.setattr(settings, "public_base_url", "https://hub.example.com")


def _webhook_calls(client, token):
    return [c[1] for c in client.fake_tg.calls if c[0] == token and c[1] in ("setWebhook", "deleteWebhook")]


def test_bot_owned_elsewhere_becomes_send_only_on_add(client, monkeypatch):
    _webhook_mode(monkeypatch)
    login(client)
    client.fake_tg.webhooks["frai-token:abc"] = PORTAL
    lane = make_lane(client, slug="frai")
    assert lane["receiveMode"] == "send_only"
    assert PORTAL in lane["warning"]
    assert _webhook_calls(client, "frai-token:abc") == []
    assert client.fake_tg.webhooks["frai-token:abc"] == PORTAL

    # sending still works through the hub
    resp = client.post("/frai/send", json={"text": "hi"}, headers={"X-Bridge-Token": lane["apiKey"]})
    assert resp.status_code == 200
    info = client.get("/frai/info", headers={"X-Bridge-Token": lane["apiKey"]}).json()
    assert info["receiveMode"] == "send_only" and info["webhookUrl"] is None
    assert info["webhook"]["url"] == PORTAL and info["webhook"]["ownedByHub"] is False


def test_send_only_lane_is_never_touched(client, monkeypatch):
    """Edit, disable, enable, delete, restart: the portal keeps its webhook."""
    import asyncio
    from app.runtime import runtime
    _webhook_mode(monkeypatch)
    login(client)
    client.fake_tg.webhooks["frai-token:abc"] = PORTAL
    make_lane(client, slug="frai")
    client.patch("/admin/api/lanes/frai", json={"title": "Frai"})
    client.patch("/admin/api/lanes/frai", json={"enabled": False})
    client.patch("/admin/api/lanes/frai", json={"enabled": True})
    asyncio.run(runtime.sync_all())
    client.delete("/admin/api/lanes/frai")
    assert _webhook_calls(client, "frai-token:abc") == []
    assert client.fake_tg.webhooks["frai-token:abc"] == PORTAL


def test_existing_lane_protected_on_startup(client, monkeypatch):
    """A 0.5.3 hub lane whose bot now points elsewhere (the Frai incident): on
    the next start the hub switches it to send-only instead of re-taking it."""
    import asyncio
    from app import db
    from app.runtime import runtime
    login(client)
    make_lane(client, slug="frai")  # delivery 'off' here, so nothing was set
    assert db.get_lane("frai")["receive_mode"] == "hub"
    client.fake_tg.webhooks["frai-token:abc"] = PORTAL
    _webhook_mode(monkeypatch)
    asyncio.run(runtime.sync_all())
    assert db.get_lane("frai")["receive_mode"] == "send_only"
    assert client.fake_tg.webhooks["frai-token:abc"] == PORTAL


def test_own_webhook_and_previous_hub_address_are_ours(client, monkeypatch):
    _webhook_mode(monkeypatch)
    login(client)
    client.fake_tg.webhooks["back-token:abc"] = "https://old-hub.example.org/back/webhook"
    lane = make_lane(client, slug="back")
    assert lane["receiveMode"] == "hub" and "warning" not in lane
    assert client.fake_tg.webhooks["back-token:abc"] == "https://hub.example.com/back/webhook"
    # delete releases OUR webhook
    client.delete("/admin/api/lanes/back")
    assert "back-token:abc" not in client.fake_tg.webhooks


def test_operator_can_explicitly_take_over(client, monkeypatch):
    _webhook_mode(monkeypatch)
    login(client)
    client.fake_tg.webhooks["frai-token:abc"] = PORTAL
    make_lane(client, slug="frai")
    resp = client.patch("/admin/api/lanes/frai", json={"receiveMode": "hub"})
    assert resp.json()["receiveMode"] == "hub"
    assert client.fake_tg.webhooks["frai-token:abc"] == "https://hub.example.com/frai/webhook"
    # and back: send-only again never touches it
    client.fake_tg.webhooks["frai-token:abc"] = PORTAL
    assert client.patch("/admin/api/lanes/frai", json={"receiveMode": "send_only"}).json()["receiveMode"] == "send_only"
    assert client.fake_tg.webhooks["frai-token:abc"] == PORTAL


def test_explicit_send_only_on_create(client, monkeypatch):
    _webhook_mode(monkeypatch)
    login(client)
    lane = make_lane(client, slug="frai", receiveMode="send_only")
    assert lane["receiveMode"] == "send_only"
    assert _webhook_calls(client, "frai-token:abc") == []


def test_root_stamps_sign_in_state(client):
    page = client.get("/")
    assert page.status_code == 200
    assert '<html lang="en" data-auth="0">' in page.text
    assert page.headers["cache-control"] == "no-store"
    login(client)
    assert '<html lang="en" data-auth="1">' in client.get("/").text


def test_add_known_bot_to_another_chat_without_token(client):
    login(client)
    first = make_lane(client, slug="alpha", chat_id="-100500")
    second = client.post("/admin/api/lanes", json={"fromLane": "alpha", "defaultChatId": "-100600"})
    assert second.status_code == 201, second.text
    lane = second.json()
    assert lane["slug"] != "alpha"
    assert lane["botUsername"] == first["botUsername"]
    assert lane["defaultChatId"] == "-100600"
    assert lane["botLanes"] == ["alpha"]
    assert lane["apiKey"] != first["apiKey"]


def test_from_lane_validation(client):
    login(client)
    make_lane(client, slug="alpha")
    both = client.post("/admin/api/lanes", json={"fromLane": "alpha", "botToken": "x:1"})
    assert both.status_code == 422
    missing = client.post("/admin/api/lanes", json={"fromLane": "nope"})
    assert missing.status_code == 404


def test_lanes_list_has_hub_wide_seen_chats(client):
    login(client)
    make_lane(client, slug="alpha")
    body = client.get("/admin/api/lanes").json()
    assert isinstance(body["seenChats"], list)
