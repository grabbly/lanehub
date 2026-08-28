"""Bridge (per-lane public API) behaviour."""
from conftest import login, make_lane

OUTGOING_BASE = 1_000_000_000_000_000


def _webhook_secret(slug):
    from app import db

    return db.get_lane(slug)["webhook_secret"]


def _push_human(client, slug, *, update_id, message_id, text, chat_id=-100500, user="alice", date=1_700_000_100):
    resp = client.post(
        f"/{slug}/webhook",
        json={
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "from": {"id": 42, "is_bot": False, "username": user},
                "chat": {"id": chat_id, "title": "Test chat", "type": "supergroup"},
                "date": date,
                "text": text,
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": _webhook_secret(slug)},
    )
    assert resp.status_code == 200, resp.text


def test_lane_auth(client):
    login(client)
    lane = make_lane(client)
    key = lane["apiKey"]

    assert client.get("/backend/messages").status_code == 401
    assert client.get("/backend/messages", headers={"X-Bridge-Token": "nope"}).status_code == 401
    assert client.get("/nosuch/messages", headers={"X-Bridge-Token": key}).status_code == 404
    resp = client.get("/backend/messages", headers={"X-Bridge-Token": key})
    assert resp.status_code == 200
    assert resp.json() == {"messages": [], "count": 0}


def test_send_records_outgoing(client):
    login(client)
    lane = make_lane(client)
    headers = {"X-Bridge-Token": lane["apiKey"]}

    resp = client.post("/backend/send", json={"text": "deploy done"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["parts"] == 1 and body["chatId"] == -100500

    msgs = client.get("/backend/messages?order=desc&limit=10", headers=headers).json()["messages"]
    assert msgs[0]["text"] == "deploy done"
    assert msgs[0]["outgoing"] is True
    assert msgs[0]["updateId"] >= OUTGOING_BASE
    assert msgs[0]["from"] == "test_bot"


def test_send_without_chat_is_503(client):
    login(client)
    lane = make_lane(client, slug="nochat", chat_id="")
    resp = client.post("/nochat/send", json={"text": "hi"}, headers={"X-Bridge-Token": lane["apiKey"]})
    assert resp.status_code == 503


def test_send_chat_override_and_snake_case_field(client):
    login(client)
    lane = make_lane(client)
    resp = client.post(
        "/backend/send",
        json={"text": "hi", "chat_id": "-200700"},
        headers={"X-Bridge-Token": lane["apiKey"]},
    )
    assert resp.status_code == 200
    assert resp.json()["chatId"] == -200700


def test_long_text_is_chunked(client):
    login(client)
    lane = make_lane(client)
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(120))  # ~12k chars
    resp = client.post("/backend/send", json={"text": text}, headers={"X-Bridge-Token": lane["apiKey"]})
    assert resp.status_code == 200
    parts = resp.json()["parts"]
    assert parts >= 3
    send_calls = [c for c in client.fake_tg.calls if c[1] == "sendMessage"]
    assert len(send_calls) == parts
    assert all(len(c[2]["text"]) <= 4000 for c in send_calls)
    joined = "".join(c[2]["text"].replace("\n", "") for c in send_calls)
    assert joined.replace(" ", "") == text.replace("\n", "").replace(" ", "")


def test_webhook_auth_and_ingest(client):
    login(client)
    lane = make_lane(client)

    resp = client.post(
        "/backend/webhook",
        json={"update_id": 1, "message": {"message_id": 1, "chat": {"id": -1}, "date": 1, "text": "x"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status_code == 403

    _push_human(client, "backend", update_id=7, message_id=501, text="привет от человека")
    msgs = client.get(
        "/backend/messages?order=desc&limit=10", headers={"X-Bridge-Token": lane["apiKey"]}
    ).json()["messages"]
    assert msgs[0]["text"] == "привет от человека"
    assert msgs[0]["from"] == "alice"
    assert msgs[0]["outgoing"] is False


def test_feed_merges_and_dedupes_lanes(client):
    login(client)
    back = make_lane(client, slug="back")
    front = make_lane(client, slug="front")

    # the same human message is captured by BOTH bots under different update_ids
    _push_human(client, "back", update_id=11, message_id=900, text="human msg", date=1_700_000_200)
    _push_human(client, "front", update_id=77, message_id=900, text="human msg", date=1_700_000_200)
    # each bot sends its own message
    client.post("/back/send", json={"text": "from back"}, headers={"X-Bridge-Token": back["apiKey"]})
    client.post("/front/send", json={"text": "from front"}, headers={"X-Bridge-Token": front["apiKey"]})

    feed = client.get(
        "/back/feed?order=desc&limit=50", headers={"X-Bridge-Token": back["apiKey"]}
    ).json()["messages"]
    texts = [m["text"] for m in feed]
    assert texts.count("human msg") == 1  # deduped across lanes
    assert "from back" in texts and "from front" in texts
    lanes = {m["lane"] for m in feed}
    assert lanes == {"back", "front"}

    # sinceDate cursor (unix seconds)
    feed2 = client.get(
        "/back/feed?sinceDate=1700000200&order=asc&limit=50",
        headers={"X-Bridge-Token": back["apiKey"]},
    ).json()["messages"]
    assert all(m["date"] > 1_700_000_200 for m in feed2)


def test_rotate_key(client):
    login(client)
    lane = make_lane(client)
    old = lane["apiKey"]
    new = client.post("/admin/api/lanes/backend/rotate-key").json()["apiKey"]
    assert new != old
    assert client.get("/backend/messages", headers={"X-Bridge-Token": old}).status_code == 401
    assert client.get("/backend/messages", headers={"X-Bridge-Token": new}).status_code == 200


def test_disabled_lane_rejects(client):
    login(client)
    lane = make_lane(client)
    client.patch("/admin/api/lanes/backend", json={"enabled": False})
    resp = client.post("/backend/send", json={"text": "hi"}, headers={"X-Bridge-Token": lane["apiKey"]})
    assert resp.status_code == 403


def test_wake_flow(client):
    login(client)
    lane = make_lane(client)
    headers = {"X-Bridge-Token": lane["apiKey"]}

    # first poll seeds the cursor to "now" and returns nothing
    w = client.get("/backend/wake", headers=headers).json()
    assert w == {"wake": False, "sessionId": None}

    # a non-mention does not wake
    _push_human(client, "backend", update_id=10, message_id=600, text="просто болтовня")
    assert client.get("/backend/wake", headers=headers).json()["wake"] is False

    # an @mention does — with the lane's stored (still empty) session id
    _push_human(client, "backend", update_id=11, message_id=601, text="эй @test_bot глянь")
    w = client.get("/backend/wake", headers=headers).json()
    assert w["wake"] is True and w["wakeId"] == 11 and w["sessionId"] is None
    assert "@test_bot" in w["text"]

    # unacked -> same wake fires again (at-least-once)
    assert client.get("/backend/wake", headers=headers).json()["wakeId"] == 11

    # ack advances the cursor and stores the session id reported by the watcher
    ack = client.post("/backend/wake/ack", json={"wakeId": 11, "sessionId": "sess-1"}, headers=headers)
    assert ack.status_code == 200 and ack.json() == {"ok": True}
    w = client.get("/backend/wake", headers=headers).json()
    assert w == {"wake": False, "sessionId": "sess-1"}

    # next mention carries the now-stored session id
    _push_human(client, "backend", update_id=12, message_id=602, text="@test_bot ещё раз")
    w = client.get("/backend/wake", headers=headers).json()
    assert w["wake"] is True and w["wakeId"] == 12 and w["sessionId"] == "sess-1"


def test_wake_ignores_history_before_first_poll(client):
    login(client)
    lane = make_lane(client)
    headers = {"X-Bridge-Token": lane["apiKey"]}

    # mention exists BEFORE the watcher ever polled -> must not be replayed
    _push_human(client, "backend", update_id=5, message_id=500, text="@test_bot старое")
    assert client.get("/backend/wake", headers=headers).json()["wake"] is False
    # and a mention arriving AFTER the seed still fires
    _push_human(client, "backend", update_id=6, message_id=501, text="@test_bot новое")
    assert client.get("/backend/wake", headers=headers).json()["wakeId"] == 6


def test_wake_mention_is_whole_token(client):
    login(client)
    lane = make_lane(client)
    headers = {"X-Bridge-Token": lane["apiKey"]}
    client.get("/backend/wake", headers=headers)  # seed
    _push_human(client, "backend", update_id=20, message_id=700, text="@test_bot2 не он")
    assert client.get("/backend/wake", headers=headers).json()["wake"] is False


def test_wake_requires_auth(client):
    login(client)
    make_lane(client)
    assert client.get("/backend/wake").status_code == 401
    assert client.post("/backend/wake/ack", json={"wakeId": 1}).status_code == 401


def test_watcher_script_served(client):
    # onboarding does `curl {hub}/watcher.py`; it must be served from the hub
    resp = client.get("/watcher.py")
    assert resp.status_code == 200
    assert "LANEHUB_BASE" in resp.text and "/wake" in resp.text


def test_info(client):
    login(client)
    lane = make_lane(client)
    _push_human(client, "backend", update_id=3, message_id=300, text="hello")
    info = client.get("/backend/info", headers={"X-Bridge-Token": lane["apiKey"]}).json()
    assert info["botUsername"] == "test_bot"
    assert info["storedMessages"] == 1
    assert info["seenChats"][0]["chatId"] == -100500
    # wake state visible before any watcher has polled
    assert info["wake"] == {"armed": False, "cursor": None, "claudeSessionId": None, "pendingMention": None}


def test_info_shows_wake_state(client):
    login(client)
    lane = make_lane(client)
    headers = {"X-Bridge-Token": lane["apiKey"]}

    client.get("/backend/wake", headers=headers)  # arm (seed cursor)
    _push_human(client, "backend", update_id=30, message_id=800, text="@test_bot глянь")

    info = client.get("/backend/info", headers=headers).json()
    assert info["wake"]["armed"] is True
    assert info["wake"]["pendingMention"] == {"wakeId": 30, "from": "alice"}
    assert info["wake"]["claudeSessionId"] is None

    client.post("/backend/wake/ack", json={"wakeId": 30, "sessionId": "sess-x"}, headers=headers)
    info = client.get("/backend/info", headers=headers).json()
    assert info["wake"]["pendingMention"] is None
    assert info["wake"]["claudeSessionId"] == "sess-x"
    assert info["wake"]["cursor"] == 30
