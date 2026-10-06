"""One bot, several chats: a lane per chat behind the bot's single webhook."""
import asyncio

from conftest import login, make_lane

TOKEN = "shared-token:abc"
HUB = "https://hub.example.com"
PORTAL = "https://portal.example.com/api/telegram/webhook"


def _webhook_mode(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "delivery_mode", "webhook")
    monkeypatch.setattr(settings, "public_base_url", HUB)


def _lane(client, slug, chat_id, **extra):
    return make_lane(client, slug=slug, chat_id=chat_id, botToken=TOKEN, **extra)


def _push(client, via, *, update_id, chat_id, text, user="alice"):
    from app import db
    resp = client.post(
        f"/{via}/webhook",
        json={
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "from": {"id": 42, "is_bot": False, "username": user},
                "chat": {"id": chat_id, "title": f"chat {chat_id}", "type": "supergroup"},
                "date": 1_700_000_000 + update_id,
                "text": text,
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": db.get_lane(via)["webhook_secret"]},
    )
    assert resp.status_code == 200, resp.text


def _feed(client, lane):
    rows = client.get(f"/{lane['slug']}/feed?order=asc", headers={"X-Bridge-Token": lane["apiKey"]}).json()
    return [m["text"] for m in rows["messages"]]


def test_same_bot_in_two_chats_both_lanes_receive(client, monkeypatch):
    from app import db
    _webhook_mode(monkeypatch)
    login(client)
    a = _lane(client, "team", "-100500")
    b = _lane(client, "lira", "-100600")
    assert a["receiveMode"] == b["receiveMode"] == "hub"
    assert "warning" not in b
    # one webhook for the bot, at the oldest lane; both cards point there
    assert client.fake_tg.webhooks[TOKEN] == f"{HUB}/team/webhook"
    assert b["webhookUrl"] == f"{HUB}/team/webhook" and b["botLanes"] == ["team"]

    _push(client, "team", update_id=1, chat_id=-100500, text="in team")
    _push(client, "team", update_id=2, chat_id=-100600, text="in lira")
    assert _feed(client, a) == ["in team"]
    assert _feed(client, b) == ["in lira"]
    assert db.count_messages("lira") == 1 and db.count_messages("team") == 1

    # each lane wakes only on its own chat
    hb = {"X-Bridge-Token": b["apiKey"]}
    ha = {"X-Bridge-Token": a["apiKey"]}
    client.get("/lira/wake", headers=hb)
    client.get("/team/wake", headers=ha)
    _push(client, "team", update_id=3, chat_id=-100600, text="@test_bot глянь")
    w = client.get("/lira/wake", headers=hb).json()
    assert w["wake"] is True and w["chatId"] == -100600
    assert client.get("/team/wake", headers=ha).json()["wake"] is False

    info = client.get("/lira/info", headers=hb).json()
    assert info["webhook"]["ownedByHub"] is True and info["webhook"]["receiverLane"] == "team"


def test_unbound_chat_stays_with_receiver_and_is_bindable_from_any_lane(client):
    login(client)
    _lane(client, "team", "-100500")
    new = _lane(client, "fresh", "")
    _push(client, "team", update_id=1, chat_id=-100700, text="hello")  # bot just added to a chat
    lanes = {lane["slug"]: lane for lane in client.get("/admin/api/lanes").json()["lanes"]}
    assert lanes["team"]["storedMessages"] == 1
    assert -100700 in [c["chatId"] for c in lanes["fresh"]["seenChats"]]
    client.patch("/admin/api/lanes/fresh", json={"defaultChatId": "-100700"})
    _push(client, "team", update_id=2, chat_id=-100700, text="now bound")
    assert _feed(client, new) == ["hello", "now bound"]


def test_upgrade_from_055_send_only_siblings_go_back_to_hub(client, monkeypatch):
    """0.5.4/0.5.5 left one lane holding the webhook and made the others
    send-only. On start the hub sees the webhook is its own and puts them all
    on hub, behind one webhook at the oldest lane."""
    from app import db
    from app.runtime import runtime
    login(client)
    _lane(client, "team", "-100500")
    _lane(client, "lira", "-100600")
    db.update_lane("team", {"receive_mode": "send_only"})
    db.set_lane_state("team", "wake_cursor", "123")
    client.fake_tg.webhooks[TOKEN] = f"{HUB}/lira/webhook"
    _webhook_mode(monkeypatch)
    asyncio.run(runtime.sync_all())
    assert db.get_lane("team")["receive_mode"] == db.get_lane("lira")["receive_mode"] == "hub"
    assert db.get_lane_state("team", "wake_cursor") is None  # reseeded in hub units
    assert client.fake_tg.webhooks[TOKEN] == f"{HUB}/team/webhook"


def test_deleting_or_disabling_the_receiver_keeps_the_others_receiving(client, monkeypatch):
    _webhook_mode(monkeypatch)
    login(client)
    _lane(client, "team", "-100500")
    _lane(client, "lira", "-100600")
    _lane(client, "third", "-100800")
    client.patch("/admin/api/lanes/team", json={"enabled": False})
    assert client.fake_tg.webhooks[TOKEN] == f"{HUB}/lira/webhook"
    client.patch("/admin/api/lanes/team", json={"enabled": True})
    assert client.fake_tg.webhooks[TOKEN] == f"{HUB}/team/webhook"
    client.delete("/admin/api/lanes/team")
    assert client.fake_tg.webhooks[TOKEN] == f"{HUB}/lira/webhook"
    client.delete("/admin/api/lanes/lira")
    assert client.fake_tg.webhooks[TOKEN] == f"{HUB}/third/webhook"
    client.delete("/admin/api/lanes/third")
    assert TOKEN not in client.fake_tg.webhooks


def test_bot_owned_elsewhere_all_lanes_send_only_and_switch_together(client, monkeypatch):
    from app import db
    _webhook_mode(monkeypatch)
    login(client)
    client.fake_tg.webhooks[TOKEN] = PORTAL
    _lane(client, "team", "-100500")
    b = _lane(client, "lira", "-100600")
    assert b["receiveMode"] == "send_only" and PORTAL in b["warning"]
    assert client.fake_tg.webhooks[TOKEN] == PORTAL
    # taking the bot over from one card takes it for every lane of the bot
    client.patch("/admin/api/lanes/lira", json={"receiveMode": "hub"})
    assert db.get_lane("team")["receive_mode"] == db.get_lane("lira")["receive_mode"] == "hub"
    assert client.fake_tg.webhooks[TOKEN] == f"{HUB}/team/webhook"
    # and giving it back
    client.fake_tg.webhooks[TOKEN] = PORTAL
    client.patch("/admin/api/lanes/team", json={"receiveMode": "send_only"})
    assert db.get_lane("team")["receive_mode"] == db.get_lane("lira")["receive_mode"] == "send_only"
    asyncio.run(__import__("app.runtime", fromlist=["runtime"]).runtime.sync_all())
    assert client.fake_tg.webhooks[TOKEN] == PORTAL


def test_operator_chat_of_a_non_receiver_lane(client):
    from app import db
    login(client)
    _lane(client, "team", "-100500")
    _lane(client, "lira", "-100600")
    client.patch("/admin/api/lanes/lira", json={"operatorChatId": "-100900"})
    _push(client, "team", update_id=1, chat_id=-100900, text="как дела?", user="owner")
    assert db.next_operator_msg("lira")["text"] == "как дела?"
    assert db.next_operator_msg("team") is None
    assert db.count_messages() == 0  # operator chat never lands in the team feed


def test_polling_runs_one_poller_per_bot(client, monkeypatch):
    from app.config import settings
    from app.runtime import runtime
    login(client)
    _lane(client, "team", "-100500")
    _lane(client, "lira", "-100600")
    monkeypatch.setattr(settings, "delivery_mode", "polling")

    async def go():
        await runtime.sync_all()
        held = sorted(runtime._pollers)
        await runtime.stop_all()
        return held

    assert asyncio.run(go()) == ["team"]


def test_hub_wide_seen_chats_list_the_lanes_that_saw_them(client):
    login(client)
    _lane(client, "team", "-100500")
    _push(client, "team", update_id=1, chat_id=-100700, text="bot added to a new group")
    seen = {c["chatId"]: c for c in client.get("/admin/api/lanes").json()["seenChats"]}
    assert seen[-100700]["lanes"] == ["team"]
