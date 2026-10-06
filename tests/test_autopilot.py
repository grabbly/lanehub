"""Tests for Autopilot mode (0.8.0)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app import db, telegram
from app.autopilot import (
    AUTO_PREFIX,
    autopilot_check_pass,
    is_autopilot_command,
    parse_autopilot_command,
)
from tests.test_admin import login, make_lane


def _headers(lane: dict) -> dict[str, str]:
    return {"X-Bridge-Token": lane["apiKey"]}


def _push(client, slug: str, update_id: int, text: str, chat_id: int = -100500, user: str = "alice", msg_id: int | None = None):
    payload = {
        "update_id": update_id,
        "message": {
            "message_id": msg_id if msg_id is not None else update_id,
            "from": {"id": 42, "is_bot": False, "username": user, "first_name": user.title()},
            "chat": {"id": chat_id, "title": "Test Group", "type": "supergroup"},
            "date": 1_700_000_000 + update_id,
            "text": text,
        },
    }
    return client.post(
        f"/{slug}/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": db.get_lane(slug)["webhook_secret"]},
    )


def test_command_parser():
    assert is_autopilot_command("/autopilot @my_bot")
    assert is_autopilot_command("/autopilot@my_bot")
    assert is_autopilot_command("/autopilot@my_bot on")
    assert is_autopilot_command("/autopilot@my_bot off")
    assert is_autopilot_command("/autopilot")
    assert is_autopilot_command("/autopilot on")
    assert is_autopilot_command("/autopilot off")
    assert is_autopilot_command("  /AUTOPILOT @Bot ON  ")
    assert not is_autopilot_command("/hello")
    assert not is_autopilot_command("/autopilot_something")

    c = parse_autopilot_command("/autopilot @my_bot on")
    assert c == {"bot_username": "my_bot", "action": "on"}

    c = parse_autopilot_command("/autopilot@my_bot off")
    assert c == {"bot_username": "my_bot", "action": "off"}

    c = parse_autopilot_command("/autopilot @my_bot")
    assert c == {"bot_username": "my_bot", "action": "on"}

    c = parse_autopilot_command("/autopilot off")
    assert c == {"bot_username": None, "action": "off"}


def test_chat_command_flow_ask_owner(client):
    login(client)
    lane = make_lane(client, slug="devbot", defaultChatId="-100500")
    db.update_lane("devbot", {"bot_username": "dev_bot"})
    # set owner
    db.set_lane_state("devbot", "autopilot_owner", "chief")
    # set operator chat
    db.set_lane_state("devbot", "operator_chat_id", "-100999")

    # User in group sends /autopilot @dev_bot
    _push(client, "devbot", update_id=1, text="/autopilot @dev_bot", chat_id=-100500, user="denis")

    # Hub should have replied in chat tagging owner AND operator chat
    calls = client.fake_tg.calls
    sent_msgs = [c for c in calls if c[1] == "sendMessage"]
    assert len(sent_msgs) >= 2

    # Group reply
    grp_msg = [c[2] for c in sent_msgs if c[2]["chat_id"] == "-100500"]
    assert grp_msg
    assert "🤖 @chief, @denis asks to switch on autopilot for @dev_bot" in grp_msg[-1]["text"]

    # Operator chat reply
    op_msg = [c[2] for c in sent_msgs if c[2]["chat_id"] == "-100999"]
    assert op_msg
    assert "🤖 @chief, @denis asks to switch on autopilot for @dev_bot" in op_msg[-1]["text"]

    # Command message is stored in the feed
    feed = client.get("/devbot/feed", headers=_headers(lane)).json()
    assert any(m["text"] == "/autopilot @dev_bot" for m in feed["messages"])

    # And does NOT trigger wake mention
    wake = client.get("/devbot/wake", headers=_headers(lane)).json()
    assert wake["wake"] is False


def test_chat_command_flow_no_owner(client):
    login(client)
    lane = make_lane(client, slug="devbot", defaultChatId="-100500")
    db.update_lane("devbot", {"bot_username": "dev_bot"})
    db.delete_lane_state("devbot", "autopilot_owner")

    _push(client, "devbot", update_id=2, text="/autopilot @dev_bot", chat_id=-100500, user="denis")

    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    assert sent_msgs
    assert "🤖 @denis asks to switch on autopilot for @dev_bot, but no owner is set in the LaneHub panel." in sent_msgs[-1]["text"]


def test_chat_command_omitted_bot_single_lane(client):
    login(client)
    lane = make_lane(client, slug="devbot", defaultChatId="-100500")
    db.update_lane("devbot", {"bot_username": "dev_bot"})
    db.set_lane_state("devbot", "autopilot_owner", "chief")

    # /autopilot without @bot in a chat with single bound lane
    _push(client, "devbot", update_id=3, text="/autopilot", chat_id=-100500, user="denis")

    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    assert sent_msgs
    assert "🤖 @chief, @denis asks to switch on autopilot for @dev_bot" in sent_msgs[-1]["text"]


def test_chat_command_omitted_bot_multi_lane_ignored(client):
    login(client)
    # two lanes bound to same chat
    make_lane(client, slug="bot1", defaultChatId="-100500")
    make_lane(client, slug="bot2", defaultChatId="-100500")

    calls_before = len(client.fake_tg.calls)
    _push(client, "bot1", update_id=4, text="/autopilot", chat_id=-100500, user="denis")
    calls_after = len(client.fake_tg.calls)
    # Ignored because @<bot> was omitted and there are 2 lanes
    assert calls_after == calls_before


def test_chat_command_off_and_status(client):
    login(client)
    lane = make_lane(client, slug="devbot", defaultChatId="-100500")
    db.update_lane("devbot", {"bot_username": "dev_bot"})
    h = _headers(lane)

    # Start autopilot
    client.post("/devbot/autopilot", headers=h, json={"on": True, "hours": 4})

    # Status request via chat command
    _push(client, "devbot", update_id=10, text="/autopilot @dev_bot", chat_id=-100500, user="denis")
    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    assert "🤖 Autopilot ON for @dev_bot until" in sent_msgs[-1]["text"]

    # Turn off via chat command
    _push(client, "devbot", update_id=11, text="/autopilot @dev_bot off", chat_id=-100500, user="denis")
    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    assert "🤖 Autopilot OFF for @dev_bot (by @denis)." in sent_msgs[-1]["text"]

    # Autopilot is now OFF
    st = client.get("/devbot/autopilot", headers=h).json()
    assert st["on"] is False


def test_autopilot_api_endpoints(client):
    login(client)
    lane = make_lane(client, slug="devbot", defaultChatId="-100500")
    db.update_lane("devbot", {"bot_username": "dev_bot"})
    db.set_lane_state("devbot", "autopilot_owner", "chief")
    h = _headers(lane)

    # Initial state
    st = client.get("/devbot/autopilot", headers=h).json()
    assert st["on"] is False
    assert st["owner"] == "chief"
    assert st["watcherOnline"] is False

    # Turn ON
    res = client.post("/devbot/autopilot", headers=h, json={"on": True, "hours": 2}).json()
    assert res["ok"] is True
    assert res["on"] is True
    assert res["until"] > int(time.time())

    # Check announced in chat
    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    assert "🤖 Autopilot ON for @dev_bot until" in sent_msgs[-1]["text"]
    assert "Mentions get automatic replies, marked 🤖 auto." in sent_msgs[-1]["text"]

    # Wake records watcher_seen
    client.get("/devbot/wake", headers=h)
    st = client.get("/devbot/autopilot", headers=h).json()
    assert st["watcherSeen"] is not None
    assert st["watcherOnline"] is True

    # Turn OFF via agent POST
    res = client.post("/devbot/autopilot", headers=h, json={"on": False}).json()
    assert res["ok"] is True
    assert res["on"] is False
    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    assert "🤖 Autopilot OFF for @dev_bot (by the agent)." in sent_msgs[-1]["text"]


def test_admin_panel_autopilot_controls(client):
    login(client)
    lane = make_lane(client, slug="devbot", defaultChatId="-100500")
    db.update_lane("devbot", {"bot_username": "dev_bot"})
    h = _headers(lane)

    # Set owner via PATCH /admin/api/lanes/{slug}
    upd = client.patch("/admin/api/lanes/devbot", json={"autopilotOwner": "@boss"}).json()
    assert upd["autopilot"]["owner"] == "boss"
    assert db.get_lane_state("devbot", "autopilot_owner") == "boss"

    # Turn ON via API
    client.post("/devbot/autopilot", headers=h, json={"on": True})

    # View reflects ON
    lanes_resp = client.get("/admin/api/lanes").json()
    lane_view = [l for l in lanes_resp["lanes"] if l["slug"] == "devbot"][0]
    assert lane_view["autopilot"]["on"] is True

    # Turning ON from panel is disallowed (422)
    resp = client.patch("/admin/api/lanes/devbot", json={"autopilot": {"on": True}})
    assert resp.status_code == 422

    # Turning OFF from panel works
    upd = client.patch("/admin/api/lanes/devbot", json={"autopilot": {"on": False}}).json()
    assert upd["autopilot"]["on"] is False
    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    assert "🤖 Autopilot OFF for @dev_bot (from the panel)." in sent_msgs[-1]["text"]


@pytest.mark.anyio
async def test_background_task_timer_and_offline(client):
    login(client)
    lane = make_lane(client, slug="devbot", defaultChatId="-100500")
    db.update_lane("devbot", {"bot_username": "dev_bot"})
    h = _headers(lane)

    now = 1_800_000_000

    # 1. Test timer expiry
    db.set_lane_state("devbot", "autopilot_until", str(now + 100))
    db.set_lane_state("devbot", "watcher_seen", str(now))

    # Before expiry: nothing happens
    actions = await autopilot_check_pass(now=now + 50)
    assert not actions

    # Past expiry: turned OFF with reason 'timer'
    actions = await autopilot_check_pass(now=now + 101)
    assert len(actions) == 1
    assert actions[0] == {"lane": "devbot", "action": "off", "reason": "timer"}

    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    assert "🤖 Autopilot OFF for @dev_bot (timer)." in sent_msgs[-1]["text"]

    # 2. Test computer went offline (> 120s without /wake)
    db.set_lane_state("devbot", "autopilot_until", str(now + 3600))
    db.set_lane_state("devbot", "autopilot_started_at", str(now))
    db.set_lane_state("devbot", "watcher_seen", str(now))

    # At 60s: still fine
    actions = await autopilot_check_pass(now=now + 60)
    assert not actions

    # At 125s without poll: turned OFF with reason 'computer went offline'
    actions = await autopilot_check_pass(now=now + 125)
    assert len(actions) == 1
    assert actions[0] == {"lane": "devbot", "action": "off", "reason": "computer went offline"}

    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    assert "🤖 Autopilot OFF for @dev_bot (computer went offline)." in sent_msgs[-1]["text"]


def test_wake_behavior_and_marking_replies(client):
    login(client)
    lane = make_lane(client, slug="devbot", defaultChatId="-100500")
    db.update_lane("devbot", {"bot_username": "dev_bot"})
    h = _headers(lane)

    # 1. When OFF: wake: false and advances cursor past mentions
    client.get("/devbot/wake", headers=h)  # seed
    _push(client, "devbot", update_id=20, text="@dev_bot old message", chat_id=-100500)
    wake = client.get("/devbot/wake", headers=h).json()
    assert wake["wake"] is False
    assert wake["autopilot"] is False
    assert wake["autopilotUntil"] is None

    # Switch ON later: that old mention must NOT be replayed!
    client.post("/devbot/autopilot", headers=h, json={"on": True})
    wake = client.get("/devbot/wake", headers=h).json()
    assert wake["wake"] is False
    assert wake["autopilot"] is True
    assert wake["autopilotUntil"] is not None

    # New mention arrives while ON
    _push(client, "devbot", update_id=21, text="@dev_bot new mention", chat_id=-100500)
    wake = client.get("/devbot/wake", headers=h).json()
    assert wake["wake"] is True
    assert wake["wakeId"] == 21
    assert wake["autopilot"] is True

    # Handing out the mention set auto_window_until
    auto_until = int(db.get_lane_state("devbot", "auto_window_until") or 0)
    assert auto_until > int(time.time())

    # While auto_window is active, /send prepends 🤖 auto ·
    client.post("/devbot/send", headers=h, json={"text": "Here is my answer"})
    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    assert sent_msgs[-1]["text"].startswith(AUTO_PREFIX)
    assert sent_msgs[-1]["text"] == "🤖 auto · Here is my answer"

    # Feed also contains the prefix
    feed = client.get("/devbot/feed", headers=h).json()
    assert any(m["text"] == "🤖 auto · Here is my answer" for m in feed["messages"])

    # Splitting long text only marks the first part
    long_text = "x" * 5000
    client.post("/devbot/send", headers=h, json={"text": long_text})
    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    # Last two messages are the split parts
    part1 = sent_msgs[-2]["text"]
    part2 = sent_msgs[-1]["text"]
    assert part1.startswith(AUTO_PREFIX)
    assert not part2.startswith(AUTO_PREFIX)

    # /sendFile caption gets marked too
    client.post(
        "/devbot/sendFile",
        headers=h,
        files={"file": ("chart.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        data={"caption": "daily chart"},
    )
    sent_files = [c for c in client.fake_tg.calls if c[1] in ("sendPhoto", "sendDocument")]
    assert sent_files[-1][2]["caption"] == "🤖 auto · daily chart"

    # /wake/ack clears auto_window_until
    client.post("/devbot/wake/ack", headers=h, json={"wakeId": 21})
    assert db.get_lane_state("devbot", "auto_window_until") is None

    # Next send does NOT get auto prefix
    client.post("/devbot/send", headers=h, json={"text": "manual message"})
    sent_msgs = [c[2] for c in client.fake_tg.calls if c[1] == "sendMessage" and c[2]["chat_id"] == "-100500"]
    assert sent_msgs[-1]["text"] == "manual message"


def test_command_handle_fits_telegram_limit():
    from app.autopilot import command_handle
    assert command_handle("gabbsrobot") == "gabbsro"
    assert command_handle("Creativeactive_bot") == "creativeactive"
    h = command_handle("frai_pocketfraiday_bot")
    assert len("autopilot_off_" + h) <= 32


def test_per_bot_commands_registered_for_the_bound_chat(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "delivery_mode", "webhook")
    monkeypatch.setattr(settings, "public_base_url", "https://hub.example.com")
    login(client)
    make_lane(client, slug="devbot")
    regs = [c[2] for c in client.fake_tg.calls if c[1] == "setMyCommands"]
    assert regs and regs[-1]["scope"] == {"type": "chat", "chat_id": "-100500"}
    names = [c["command"] for c in regs[-1]["commands"]]
    assert names == ["autopilot_on_test", "autopilot_off_test"]


def test_send_only_lane_registers_no_commands(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "delivery_mode", "webhook")
    monkeypatch.setattr(settings, "public_base_url", "https://hub.example.com")
    login(client)
    make_lane(client, slug="frai", receiveMode="send_only")
    assert not [c for c in client.fake_tg.calls if c[1] == "setMyCommands"]


def test_one_tap_off_command_with_appended_bot_name(client):
    login(client)
    lane = make_lane(client, slug="devbot")
    h = {"X-Bridge-Token": lane["apiKey"]}
    client.post("/devbot/autopilot", headers=h, json={"on": True})
    _push(client, "devbot", update_id=20, text="/autopilot_off_test@test_bot", user="denis")
    assert client.get("/devbot/autopilot", headers=h).json()["on"] is False


def test_same_command_seen_by_two_bots_is_handled_once(client):
    login(client)
    lane = make_lane(client, slug="devbot")
    h = {"X-Bridge-Token": lane["apiKey"]}
    client.post("/devbot/autopilot", headers=h, json={"on": True})
    before = len([c for c in client.fake_tg.calls if c[1] == "sendMessage"])
    for update_id in (30, 31):  # same Telegram message delivered twice
        _push(client, "devbot", update_id=update_id, text="/autopilot_off_test", msg_id=777)
    offs = [c for c in client.fake_tg.calls[before:] if c[1] == "sendMessage" and "OFF" in c[2]["text"]]
    assert len(offs) == 1


def test_busy_watcher_is_not_offline(client):
    import asyncio, time
    from app.autopilot import autopilot_check_pass
    login(client)
    lane = make_lane(client, slug="devbot")
    h = {"X-Bridge-Token": lane["apiKey"]}
    client.post("/devbot/autopilot", headers=h, json={"on": True})
    now = int(time.time())
    db.set_lane_state("devbot", "watcher_seen", str(now - 600))
    db.set_lane_state("devbot", "autopilot_started_at", str(now - 600))
    db.set_lane_state("devbot", "auto_window_until", str(now + 600))  # claude is answering a mention
    asyncio.run(autopilot_check_pass(now))
    assert client.get("/devbot/autopilot", headers=h).json()["on"] is True
    db.delete_lane_state("devbot", "auto_window_until")
    asyncio.run(autopilot_check_pass(now))
    assert client.get("/devbot/autopilot", headers=h).json()["on"] is False
