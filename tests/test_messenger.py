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


# --- photos ------------------------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body-1" * 10
JPEG2 = b"\xff\xd8\xff\xe0" + b"jpeg-body-2" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"png-body" * 10


def _photo(client, lane, content, name="p.jpg", mime="image/jpeg", chat=THREAD):
    return client.post(f"/{lane['slug']}/messenger/photo", headers=_h(lane),
                       data={"chatId": chat}, files={"file": (name, content, mime)})


def _feed(client, lane):
    return client.get(f"/{lane['slug']}/feed?order=asc", headers=_h(lane)).json()["messages"]


def test_photo_upload_checks_thread_size_and_type(client):
    lane = make_messenger_lane(client)
    assert _photo(client, lane, JPEG, chat="123456789").status_code == 403
    assert _photo(client, lane, b"").status_code == 422
    r = _photo(client, lane, b"<html>not a picture</html>", mime="image/jpeg")
    assert r.status_code == 415  # the bytes decide, not the declared type
    r = _photo(client, lane, PNG, name="x.bin", mime="application/octet-stream")
    assert r.status_code == 200, r.text
    assert r.json()["mime"] == "image/png" and r.json()["size"] == len(PNG)
    assert r.json()["fileId"].startswith("mx_")
    # uploaded but not in any message yet: not downloadable
    assert client.get(f"/fbgroup/file/{r.json()['fileId']}", headers=_h(lane)).status_code == 404


def test_single_photo_lands_in_feed_and_downloads(client):
    lane = make_messenger_lane(client)
    fid = _photo(client, lane, JPEG).json()["fileId"]
    _ingest(client, lane, [{"id": "p1", "from": "Justyna", "kind": "photo", "text": "",
                            "photos": [{"fileId": fid, "width": 1600, "height": 1200}]}])
    rows = _feed(client, lane)
    assert len(rows) == 1
    row = rows[0]
    assert row["text"] == "[photo]"
    media = row["media"]
    assert media["kind"] == "photo" and media["fileId"] == fid
    assert media["mime"] == "image/jpeg" and media["size"] == len(JPEG)
    assert media["name"].startswith("messenger-") and media["name"].endswith("-1.jpg")
    assert media["width"] == 1600 and media["height"] == 1200
    got = client.get(f"/fbgroup/file/{fid}", headers=_h(lane))
    assert got.status_code == 200 and got.content == JPEG
    assert got.headers["content-type"].startswith("image/jpeg")
    assert media["name"] in got.headers["content-disposition"]


def test_several_photos_become_one_row_each(client):
    lane = make_messenger_lane(client)
    ids = [_photo(client, lane, c).json()["fileId"] for c in (JPEG, JPEG2, PNG)]
    _ingest(client, lane, [
        {"id": "a1", "from": "Justyna", "kind": "photo", "text": "remove these",
         "photos": [{"fileId": f} for f in ids]},
        {"id": "a2", "from": "Bartek", "text": "ok"},
    ])
    rows = _feed(client, lane)
    assert [r["text"] for r in rows] == ["[photo 1/3] remove these", "[photo 2/3]", "[photo 3/3]", "ok"]
    assert [r["media"]["fileId"] for r in rows[:3]] == ids
    assert [r["media"]["index"] for r in rows[:3]] == [1, 2, 3]
    assert {r["media"]["count"] for r in rows[:3]} == {3}
    assert [r["from"] for r in rows[:3]] == ["Justyna"] * 3
    assert rows[2]["media"]["name"].endswith("-3.png")
    assert len({r["updateId"] for r in rows}) == 4  # tg-fetch.sh dedupes by lane:update_id
    for fid, content in zip(ids, (JPEG, JPEG2, PNG)):
        assert client.get(f"/fbgroup/file/{fid}", headers=_h(lane)).content == content
    # reading the message again (page reload) duplicates nothing and keeps media
    _ingest(client, lane, [{"id": "a1", "from": "Justyna", "kind": "photo", "text": "remove these",
                            "photos": [{"fileId": f} for f in ids]}], backfill=True)
    _ingest(client, lane, [{"id": "a1", "from": "Justyna", "kind": "photo", "text": "remove these"}])
    again = _feed(client, lane)
    assert [r["text"] for r in again] == [r["text"] for r in rows]
    assert again[0]["media"]["fileId"] == ids[0]


def test_photos_arriving_after_their_message_get_rows_of_their_own(client):
    # The 0.1 extension stored photo messages as "[photo]" without media; a
    # feed reader may already have that row, so it is not changed afterwards.
    lane = make_messenger_lane(client)
    _ingest(client, lane, [{"id": "late", "from": "Justyna", "text": "[photo]"}])
    before = _feed(client, lane)
    assert before[0]["media"] is None
    fid = _photo(client, lane, JPEG).json()["fileId"]
    _ingest(client, lane, [{"id": "late", "from": "Justyna", "kind": "photo", "text": "",
                            "photos": [{"fileId": fid}]}], backfill=True)
    rows = _feed(client, lane)
    assert len(rows) == 2
    assert rows[0]["media"] is None and rows[0]["text"] == "[photo]"
    assert rows[1]["media"]["fileId"] == fid and rows[1]["text"] == "[photo]"
    assert rows[1]["seq"] > before[0]["seq"]  # a cursor reader picks it up
    assert rows[1].get("history") is None  # follows its message, which was live


def test_unknown_or_foreign_photo_ids_are_ignored(client):
    lane = make_messenger_lane(client)
    _ingest(client, lane, [{"id": "x1", "from": "Anna", "kind": "photo", "text": "",
                            "photos": [{"fileId": "mx_" + "0" * 32}, {"fileId": "../../hub.db"}]}])
    rows = _feed(client, lane)
    assert len(rows) == 1 and rows[0]["media"] is None and rows[0]["text"] == "[photo]"


def test_photo_file_is_private_to_lanes_of_its_chat(client):
    lane = make_messenger_lane(client)
    other = make_messenger_lane(client, slug="other", chat="https://www.messenger.com/t/1234567890")
    fid = _photo(client, lane, JPEG).json()["fileId"]
    _ingest(client, lane, [{"id": "s1", "from": "Anna", "kind": "photo", "photos": [{"fileId": fid}]}])
    assert client.get(f"/fbgroup/file/{fid}", headers=_h(lane)).status_code == 200
    assert client.get(f"/other/file/{fid}", headers=_h(other)).status_code == 404


def test_photo_mention_in_caption_wakes(client):
    lane = make_messenger_lane(client)
    client.get("/fbgroup/wake", headers=_h(lane))
    client.post("/fbgroup/autopilot", headers=_h(lane), json={"on": True, "hours": 1})
    fid = _photo(client, lane, JPEG).json()["fileId"]
    _ingest(client, lane, [{"id": "w1", "from": "Justyna", "kind": "photo", "text": "@Lane Bot this one",
                            "photos": [{"fileId": fid}]}])
    w = client.get("/fbgroup/wake", headers=_h(lane)).json()
    assert w["wake"] is True and w["text"] == "[photo] @Lane Bot this one"
