"""Admin API: auth, lane management, feed, send."""
from conftest import ADMIN_PASSWORD, login, make_lane


def test_admin_requires_login(client):
    assert client.get("/admin/api/lanes").status_code == 401
    assert client.post("/admin/api/lanes", json={"slug": "x", "botToken": "t"}).status_code == 401


def test_wrong_password(client):
    resp = client.post("/admin/api/login", json={"password": "nope"})
    assert resp.status_code == 401


def test_status_reflects_auth(client):
    assert client.get("/admin/api/status").json()["authenticated"] is False
    login(client)
    st = client.get("/admin/api/status").json()
    assert st["authenticated"] is True
    assert st["adminPasswordSet"] is True


def test_lane_validation(client):
    login(client)
    bad = client.post("/admin/api/lanes", json={"slug": "Bad Slug!", "botToken": "t:1"})
    assert bad.status_code == 422
    reserved = client.post("/admin/api/lanes", json={"slug": "admin", "botToken": "t:1"})
    assert reserved.status_code == 422
    make_lane(client, slug="dup")
    again = client.post("/admin/api/lanes", json={"slug": "dup", "botToken": "t:1"})
    assert again.status_code == 409


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
    client.post("/admin/api/logout")
    assert client.get("/admin/api/lanes").status_code == 401


def test_history_survives_lane_delete(client):
    login(client)
    make_lane(client, slug="temp")
    client.post("/admin/api/lanes/temp/send", json={"text": "keep me"})
    client.delete("/admin/api/lanes/temp")
    feed = client.get("/admin/api/feed").json()
    assert any(m["text"] == "keep me" for m in feed["messages"])
