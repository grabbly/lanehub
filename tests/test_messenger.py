from app import db
from app.config import settings
from app.runtime import runtime
from tests.conftest import login

THREAD = "29179688748328713"


def make_messenger_lane(client, slug="fbgroup", chat=f"https://www.messenger.com/t/{THREAD}/", name="Lane Bot"):
    login(client)
    resp = client.post("/admin/api/lanes", json={
        "slug": slug, "transport": "messenger", "botName": name, "defaultChatId": chat,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _h(lane):
    return {"X-Bridge-Token": lane["apiKey"]}


def _ingest(client, lane, messages, chat=THREAD, backfill=False):
    resp = client.post(f"/{lane['slug']}/messenger/ingest", headers=_h(lane),
                       json={"chatId": chat, "chatTitle": "Team", "messages": messages, "backfill": backfill})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_messenger_lane_touches_no_telegram(client):
    lane = make_messenger_lane(client)
    assert lane["transport"] == "messenger"
    assert lane["defaultChatId"] == THREAD  # the URL is reduced to the thread id
    assert lane["botUsername"] == "Lane Bot"
    assert client.fake_tg.calls == []
    info = client.get("/fbgroup/info", headers=_h(lane)).json()
    assert info["transport"] == "messenger"
    assert info["threadUrl"].endswith(THREAD)
    assert client.fake_tg.calls == []


def test_create_messenger_lane_validation(client):
    login(client)
    r = client.post("/admin/api/lanes", json={"slug": "x", "transport": "messenger", "defaultChatId": THREAD})
    assert r.status_code == 422  # botName required
    r = client.post("/admin/api/lanes", json={"slug": "x", "transport": "messenger", "botName": "B",
                                              "defaultChatId": "not a thread"})
    assert r.status_code == 422


def test_ingest_is_idempotent_and_feeds_messages(client):
    lane = make_messenger_lane(client)
    msgs = [{"id": "m1", "from": "Anna", "text": "hello"}, {"id": "m2", "from": "Lane Bot", "text": "hi",
                                                             "outgoing": True}]
    first = _ingest(client, lane, msgs)
    assert first["new"] == 2
    again = _ingest(client, lane, [{"id": "m1", "from": "Anna", "text": "hello (edited)"}])
    assert again["new"] == 0
    rows = client.get("/fbgroup/messages", headers=_h(lane)).json()["messages"]
    assert [r["text"] for r in rows] == ["hello (edited)", "hi"]
    assert [r["outgoing"] for r in rows] == [False, True]
    assert all(r["updateId"] < db.OUTGOING_BASE for r in rows)
    feed = client.get("/fbgroup/feed", headers=_h(lane)).json()["messages"]
    assert len(feed) == 2 and feed[0]["chatId"] == int(THREAD)


def test_ingest_refuses_other_thread_and_telegram_lanes(client):
    lane = make_messenger_lane(client)
    r = client.post("/fbgroup/messenger/ingest", headers=_h(lane),
                    json={"chatId": "123456789", "messages": []})
    assert r.status_code == 403
    r = client.post("/fbgroup/messenger/ingest", headers={"X-Bridge-Token": "wrong"},
                    json={"chatId": THREAD, "messages": []})
    assert r.status_code == 401
    tg = client.post("/admin/api/lanes", json={"slug": "tg", "botToken": "tg-token:abc",
                                               "defaultChatId": "-100500"}).json()
    r = client.post("/tg/messenger/ingest", headers=_h(tg), json={"chatId": THREAD, "messages": []})
    assert r.status_code == 400


def test_send_becomes_draft_and_is_closed_when_seen(client):
    lane = make_messenger_lane(client)
    calls_before = len(client.fake_tg.calls)
    r = client.post("/fbgroup/send", headers=_h(lane), json={"text": "On it,  will check"})
    assert r.status_code == 200, r.text
    assert r.json()["draft"] is True
    assert len(client.fake_tg.calls) == calls_before  # nothing went to Telegram
    drafts = client.get("/fbgroup/messenger/drafts", headers=_h(lane)).json()["drafts"]
    assert [d["status"] for d in drafts] == ["pending"]
    did = drafts[0]["id"]
    assert client.post(f"/fbgroup/messenger/drafts/{did}", headers=_h(lane),
                       json={"status": "filled"}).status_code == 200
    # the person pressed Send: the extension reads the bot account's message
    _ingest(client, lane, [{"id": "m9", "from": "Lane Bot", "text": "On it, will check", "outgoing": True}])
    assert client.get("/fbgroup/messenger/drafts", headers=_h(lane)).json()["drafts"] == []
    r = client.post("/fbgroup/sendFile", headers=_h(lane), files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 501


def test_wake_on_mention_of_bot_account_name(client):
    lane = make_messenger_lane(client)
    _ingest(client, lane, [{"id": "old", "from": "Anna", "text": "before the watcher"}])
    assert client.get("/fbgroup/wake", headers=_h(lane)).json()["wake"] is False  # seeds the cursor
    client.post("/fbgroup/autopilot", headers=_h(lane), json={"on": True, "hours": 1})
    _ingest(client, lane, [
        {"id": "a", "from": "Anna", "text": "just chatting"},
        {"id": "b", "from": "Lane Bot", "text": "@Lane Bot echo", "outgoing": True},
        {"id": "c", "from": "Anna", "text": "@Lane Bot can you look?"},
    ])
    w = client.get("/fbgroup/wake", headers=_h(lane)).json()
    assert w["wake"] is True
    assert w["text"] == "@Lane Bot can you look?"
    assert w["from"] == "Anna"
    assert client.fake_tg.calls == []  # autopilot isn't announced through Telegram


def test_backfilled_history_never_wakes(client):
    lane = make_messenger_lane(client)
    client.get("/fbgroup/wake", headers=_h(lane))  # watcher armed, nothing stored yet
    client.post("/fbgroup/autopilot", headers=_h(lane), json={"on": True, "hours": 1})
    _ingest(client, lane, [{"id": "h1", "from": "Anna", "text": "@Lane Bot old question"}], backfill=True)
    assert client.get("/fbgroup/wake", headers=_h(lane)).json()["wake"] is False
    _ingest(client, lane, [{"id": "n1", "from": "Anna", "text": "@Lane Bot new question"}])
    _ingest(client, lane, [{"id": "h0", "from": "Anna", "text": "@Lane Bot older"}], backfill=True)
    w = client.get("/fbgroup/wake", headers=_h(lane)).json()
    # the live mention still waits, an older one scrolled into view doesn't jump the queue
    assert w["wake"] is True and w["text"] == "@Lane Bot new question"


def test_voice_clip_stored_served_and_transcribed(client, monkeypatch):
    monkeypatch.setattr(settings, "stt_url", "http://stt.invalid/transcribe")
    lane = make_messenger_lane(client)
    r = client.post("/fbgroup/messenger/voice", headers=_h(lane),
                    data={"messageId": "v1", "chatId": THREAD, "duration": "7"},
                    files={"file": ("clip.mp4", b"AUDIO", "audio/mp4")})
    assert r.status_code == 404  # the message must be ingested first
    _ingest(client, lane, [{"id": "v1", "from": "Anna", "kind": "voice"}])
    r = client.post("/fbgroup/messenger/voice", headers=_h(lane),
                    data={"messageId": "v1", "chatId": THREAD, "duration": "7"},
                    files={"file": ("clip.mp4", b"AUDIO", "audio/mp4")})
    assert r.status_code == 200, r.text
    fid = r.json()["media"]["fileId"]
    got = client.get(f"/fbgroup/file/{fid}", headers=_h(lane))
    assert got.status_code == 200 and got.content == b"AUDIO"
    assert got.headers["content-type"].startswith("audio/mp4")

    seen = {}

    async def fake_transcribe(data, filename, mime):
        seen.update(data=data, filename=filename, mime=mime)
        return {"text": "call me later"}

    monkeypatch.setattr("app.stt.transcribe", fake_transcribe)
    import asyncio
    assert asyncio.run(runtime.stt_step()) is True
    assert seen["data"] == b"AUDIO" and seen["mime"] == "audio/mp4"
    rows = client.get("/fbgroup/messages", headers=_h(lane)).json()["messages"]
    assert rows[0]["text"] == "[voice 0:07] call me later"
    # re-reading the message (no media in the payload) keeps the clip
    _ingest(client, lane, [{"id": "v1", "from": "Anna", "kind": "voice"}])
    rows = client.get("/fbgroup/messages", headers=_h(lane)).json()["messages"]
    assert rows[0]["media"]["fileId"] == fid
    assert client.get("/fbgroup/file/mx_" + "0" * 32, headers=_h(lane)).status_code == 404


def test_edit_and_delete_messenger_lane(client):
    lane = make_messenger_lane(client)
    r = client.patch("/admin/api/lanes/fbgroup", json={"botName": "Helper", "defaultChatId": "1234567890"})
    assert r.status_code == 200, r.text
    assert r.json()["botUsername"] == "Helper" and r.json()["defaultChatId"] == "1234567890"
    assert client.patch("/admin/api/lanes/fbgroup", json={"botToken": "x:y"}).status_code == 422
    assert client.get("/admin/api/lanes/fbgroup/webhook-info").status_code == 422
    assert client.delete("/admin/api/lanes/fbgroup").status_code == 200
    assert client.fake_tg.calls == []
    assert lane["slug"] == "fbgroup"


def test_mention_without_at_sign():
    from app.messenger import mentions
    assert mentions("Lane Bot can you check?", "Lane Bot")
    assert mentions("hey @lane bot", "Lane Bot")
    assert not mentions("Lane Botany notes", "Lane Bot")
    assert not mentions("ask Lane", "Lane Bot")
