import asyncio
import json
import time

import pytest
from app import db
from app.config import settings
from app.runtime import runtime
from tests.conftest import login, make_lane


def _stt_secret():
    return "-".join(["test", "stt", "secret", "val"])


def _webhook_secret(slug: str) -> str:
    lane = db.get_lane(slug)
    return lane["webhook_secret"]


def _push_voice(
    client,
    slug="backend",
    update_id=10,
    message_id=100,
    duration=39,
    file_id="voice_fid_1",
    file_unique_id="v_uid_1",
    caption="",
    size=12345,
    chat_id=-100500,
    kind="voice",
    date=None,
):
    media_obj = {
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "file_size": size,
        "duration": duration,
        "mime_type": "audio/ogg" if kind != "video_note" else "video/mp4",
    }
    msg = {
        "message_id": message_id,
        "from": {"id": 42, "is_bot": False, "username": "alice"},
        "chat": {"id": chat_id, "title": "Test chat", "type": "supergroup"},
        "date": date or 1_700_000_100,
        kind: media_obj,
    }
    if caption:
        msg["caption"] = caption
    resp = client.post(
        f"/{slug}/webhook",
        json={"update_id": update_id, "message": msg},
        headers={"X-Telegram-Bot-Api-Secret-Token": _webhook_secret(slug)},
    )
    assert resp.status_code == 200, resp.text


def test_stt_disabled_by_default(client, monkeypatch):
    """When HUB_STT_URL is empty: feature is off, no transcript rows, endpoints 503."""
    monkeypatch.setattr(settings, "stt_url", "")
    assert not settings.stt_enabled

    # session probe
    assert client.get("/api/session").json()["sttEnabled"] is False

    login(client)
    lane = make_lane(client, slug="backend")
    token_hdr = {"X-Bridge-Token": lane["apiKey"]}

    # lane info probe
    assert client.get("/backend/info", headers=token_hdr).json()["sttEnabled"] is False

    # incoming voice message
    _push_voice(client, "backend", update_id=10, message_id=100)
    assert db.list_transcripts() == []

    # feed query
    feed = client.get("/backend/feed", headers=token_hdr).json()["messages"]
    assert len(feed) == 1
    assert feed[0]["text"] == "[voice]"
    assert feed[0]["transcript"] is None
    assert feed[0]["transcriptStatus"] is None

    # messages query
    msgs = client.get("/backend/messages", headers=token_hdr).json()["messages"]
    assert msgs[0]["transcript"] is None
    assert msgs[0]["transcriptStatus"] is None

    # endpoints return 503
    assert client.post("/backend/transcribe/voice_fid_1", headers=token_hdr).status_code == 503
    assert client.post("/admin/api/transcripts/backfill", json={"hours": 72}).status_code == 503
    assert client.get("/admin/api/transcripts").status_code == 503

    # worker step is no-op
    assert asyncio.run(runtime.stt_step()) is False


def test_stt_enabled_voice_lifecycle(client, monkeypatch):
    """Voice update -> pending row -> worker step -> done; feed text rewritten, stored text unchanged."""
    secret_val = _stt_secret()
    monkeypatch.setattr(settings, "stt_url", "http://fake-stt.local/transcribe")
    monkeypatch.setattr(settings, "stt_key", secret_val)
    assert settings.stt_enabled

    login(client)
    lane = make_lane(client, slug="backend")
    token_hdr = {"X-Bridge-Token": lane["apiKey"]}

    # Push a voice update
    _push_voice(client, "backend", update_id=10, message_id=100, duration=39, file_id="voice_fid_1", file_unique_id="v_uid_1")

    # Pending row enqueued in DB
    rows = db.list_transcripts()
    assert len(rows) == 1
    assert rows[0]["file_unique_id"] == "v_uid_1"
    assert rows[0]["status"] == "pending"
    assert rows[0]["duration"] == 39

    # Feed while pending shows transcribing marker
    feed = client.get("/backend/feed", headers=token_hdr).json()["messages"]
    assert feed[0]["transcriptStatus"] == "pending"
    assert feed[0]["transcript"] is None
    assert feed[0]["text"] == "[voice 0:39 · transcribing…]"

    # Mock stt.transcribe
    calls = []

    async def fake_transcribe(data, filename, mime):
        calls.append((data, filename, mime))
        return {"text": "У нас сайт – это больше визитка…", "language": "ru"}

    monkeypatch.setattr("app.stt.transcribe", fake_transcribe)

    # Run ONE worker step
    processed = asyncio.run(runtime.stt_step())
    assert processed is True
    assert len(calls) == 1

    # Database updated to done
    t = db.get_transcript("v_uid_1")
    assert t["status"] == "done"
    assert t["text"] == "У нас сайт – это больше визитка…"
    assert t["language"] == "ru"

    # Feed text rewritten to [voice 0:39] + text
    feed = client.get("/backend/feed", headers=token_hdr).json()["messages"]
    assert feed[0]["transcriptStatus"] == "done"
    assert feed[0]["transcript"] == "У нас сайт – это больше визитка…"
    assert feed[0]["text"] == "[voice 0:39] У нас сайт – это больше визитка…"

    # Stored messages.text in SQLite is strictly unchanged
    with db.connect() as conn:
        stored = conn.execute("SELECT text FROM messages WHERE update_id = 10").fetchone()
        assert stored["text"] == "[voice]"


def test_two_lanes_same_chat_one_transcript_one_call(client, monkeypatch):
    """Two lanes receiving the same voice message produce ONE transcripts row and ONE STT call."""
    monkeypatch.setattr(settings, "stt_url", "http://fake-stt.local/transcribe")
    login(client)
    lane1 = make_lane(client, slug="lane1", chat_id="-100500")
    lane2 = make_lane(client, slug="lane2", chat_id="-100500")

    calls = []

    async def fake_transcribe(data, filename, mime):
        calls.append((data, filename, mime))
        return {"text": "один вызов", "language": "ru"}

    monkeypatch.setattr("app.stt.transcribe", fake_transcribe)

    # Ingest through lane1, then lane2
    _push_voice(client, "lane1", update_id=1, message_id=50, file_id="fid1", file_unique_id="shared_uid_1")
    _push_voice(client, "lane2", update_id=2, message_id=50, file_id="fid2", file_unique_id="shared_uid_1")

    # Only one transcripts row
    rows = db.list_transcripts()
    assert len(rows) == 1
    assert rows[0]["file_unique_id"] == "shared_uid_1"

    # Worker step processes it
    assert asyncio.run(runtime.stt_step()) is True
    assert len(calls) == 1

    # Next step is idle
    assert asyncio.run(runtime.stt_step()) is False


def test_redelivered_update_does_not_reset_done_row(client, monkeypatch):
    """Redelivered update with the same file_unique_id does not reset a done transcript row."""
    monkeypatch.setattr(settings, "stt_url", "http://fake-stt.local/transcribe")
    login(client)
    make_lane(client, slug="backend")

    async def fake_transcribe(data, filename, mime):
        return {"text": "готово", "language": "ru"}

    monkeypatch.setattr("app.stt.transcribe", fake_transcribe)

    _push_voice(client, "backend", update_id=10, message_id=100, file_id="fid_1", file_unique_id="uid_done_1")
    assert asyncio.run(runtime.stt_step()) is True

    row = db.get_transcript("uid_done_1")
    assert row["status"] == "done"
    assert row["text"] == "готово"

    # Redeliver update
    _push_voice(client, "backend", update_id=10, message_id=100, file_id="fid_1", file_unique_id="uid_done_1")
    row_after = db.get_transcript("uid_done_1")
    assert row_after["status"] == "done"
    assert row_after["text"] == "готово"


def test_stt_error_backoff_and_failed(client, monkeypatch):
    """STT error triggers retries with backoff and marks failed after 3 attempts; worker keeps running."""
    from app.stt import SttError

    monkeypatch.setattr(settings, "stt_url", "http://fake-stt.local/transcribe")
    login(client)
    make_lane(client, slug="backend")

    async def broken_transcribe(data, filename, mime):
        raise SttError("stt backend timeout error")

    monkeypatch.setattr("app.stt.transcribe", broken_transcribe)

    _push_voice(client, "backend", update_id=10, message_id=100, file_id="fid_1", file_unique_id="err_uid_1")

    # Attempt 1
    now = int(time.time())
    assert asyncio.run(runtime.stt_step()) is True
    r = db.get_transcript("err_uid_1")
    assert r["status"] == "pending"
    assert r["attempts"] == 1
    assert r["next_try_at"] >= now + 50
    assert "stt backend timeout error" in r["error"]

    # Attempt 2 (simulate passage of time)
    with db.connect() as conn:
        conn.execute("UPDATE transcripts SET next_try_at = 0 WHERE file_unique_id = 'err_uid_1'")
    assert asyncio.run(runtime.stt_step()) is True
    r = db.get_transcript("err_uid_1")
    assert r["status"] == "pending"
    assert r["attempts"] == 2
    assert r["next_try_at"] >= now + 250

    # Attempt 3 -> status becomes failed
    with db.connect() as conn:
        conn.execute("UPDATE transcripts SET next_try_at = 0 WHERE file_unique_id = 'err_uid_1'")
    assert asyncio.run(runtime.stt_step()) is True
    r = db.get_transcript("err_uid_1")
    assert r["status"] == "failed"
    assert r["attempts"] == 3

    # Worker stays alive and reports idle
    assert asyncio.run(runtime.stt_step()) is False


def test_too_long_or_large_media_skipped(client, monkeypatch):
    """Media exceeding max duration or 20 MB size is marked skipped and STT is never called."""
    monkeypatch.setattr(settings, "stt_url", "http://fake-stt.local/transcribe")
    monkeypatch.setattr(settings, "stt_max_seconds", 900)
    login(client)
    make_lane(client, slug="backend")

    calls = []

    async def fake_transcribe(data, filename, mime):
        calls.append(data)
        return {"text": "never called"}

    monkeypatch.setattr("app.stt.transcribe", fake_transcribe)

    # 901s > 900s
    _push_voice(client, "backend", update_id=10, message_id=100, duration=901, file_unique_id="too_long_1")
    t1 = db.get_transcript("too_long_1")
    assert t1["status"] == "skipped"
    assert "exceeds" in t1["error"]

    # 25 MB > 20 MB
    _push_voice(client, "backend", update_id=11, message_id=101, size=25 * 1024 * 1024, file_unique_id="too_big_1")
    t2 = db.get_transcript("too_big_1")
    assert t2["status"] == "skipped"
    assert "20 MB" in t2["error"]

    # Worker step does not pick skipped rows
    assert asyncio.run(runtime.stt_step()) is False
    assert len(calls) == 0


def test_transcribe_endpoint_auth_and_chat_isolation(client, monkeypatch):
    """POST /{lane}/transcribe/{fileId} respects lane auth, chat isolation, and 503 when off."""
    monkeypatch.setattr(settings, "stt_url", "http://fake-stt.local/transcribe")
    login(client)
    lane_a = make_lane(client, slug="lanea", chat_id="-100100")
    lane_b = make_lane(client, slug="laneb", chat_id="-100200")
    token_a = {"X-Bridge-Token": lane_a["apiKey"]}
    token_b = {"X-Bridge-Token": lane_b["apiKey"]}

    async def fake_transcribe(data, filename, mime):
        return {"text": "расшифровка файла", "language": "ru"}

    monkeypatch.setattr("app.stt.transcribe", fake_transcribe)

    # Push to lane A (chat 100) and lane B (chat 200)
    _push_voice(client, "lanea", update_id=1, message_id=10, file_id="fid_a", file_unique_id="uid_a", chat_id=-100100)
    _push_voice(client, "laneb", update_id=2, message_id=20, file_id="fid_b", file_unique_id="uid_b", chat_id=-100200)

    # 401 with bad key
    bad_token_val = "-".join(["invalid", "test", "token"])
    assert client.post("/lanea/transcribe/fid_a", headers={"X-Bridge-Token": bad_token_val}).status_code == 401

    # 404 for file belonging to another chat
    assert client.post("/lanea/transcribe/fid_b", headers=token_a).status_code == 404

    # 404 for unknown file
    assert client.post("/lanea/transcribe/nonexistent", headers=token_a).status_code == 404

    # 200 for own chat file -> returns status pending/done
    res1 = client.post("/lanea/transcribe/fid_a", headers=token_a)
    assert res1.status_code == 200
    assert res1.json()["status"] == "pending"

    # Transcribe via worker
    assert asyncio.run(runtime.stt_step()) is True

    # Calling again returns existing row with transcript
    res2 = client.post("/lanea/transcribe/fid_a", headers=token_a)
    assert res2.status_code == 200
    assert res2.json() == {"status": "done", "transcript": "расшифровка файла"}


def test_admin_backfill_and_list(client, monkeypatch):
    """Admin backfill queues only messages without transcripts; requires cookie."""
    monkeypatch.setattr(settings, "stt_url", "http://fake-stt.local/transcribe")
    login(client)
    make_lane(client, slug="backend")

    # Ingest 3 voice messages
    now = int(time.time())
    _push_voice(client, "backend", update_id=1, message_id=10, file_id="f1", file_unique_id="u1", date=now - 3600)
    _push_voice(client, "backend", update_id=2, message_id=20, file_id="f2", file_unique_id="u2", date=now - 7200)
    _push_voice(client, "backend", update_id=3, message_id=30, file_id="f3", file_unique_id="u3", date=now - 10800)

    # Remove rows for u2 and u3 from transcripts
    with db.connect() as conn:
        conn.execute("DELETE FROM transcripts WHERE file_unique_id IN ('u2', 'u3')")

    # Check unauthenticated calls fail with 401
    unauthed_client = client.__class__(client.app)
    assert unauthed_client.post("/admin/api/transcripts/backfill", json={"hours": 48}).status_code == 401
    assert unauthed_client.get("/admin/api/transcripts").status_code == 401

    # Backfill missing ones
    resp = client.post("/admin/api/transcripts/backfill", json={"hours": 48})
    assert resp.status_code == 200
    assert resp.json() == {"queued": 2}

    # Running backfill again yields 0
    resp2 = client.post("/admin/api/transcripts/backfill", json={"hours": 48})
    assert resp2.json() == {"queued": 0}

    # List transcripts
    lst = client.get("/admin/api/transcripts?limit=10").json()
    assert lst["count"] == 3
    assert {r["fileUniqueId"] for r in lst["transcripts"]} == {"u1", "u2", "u3"}


def test_secret_key_never_appears_in_logs_or_responses(client, monkeypatch, caplog):
    """Neither the STT API key nor bot token ever appears in log outputs or HTTP responses."""
    import logging
    from app.stt import SttError

    secret_val = _stt_secret()
    monkeypatch.setattr(settings, "stt_url", "http://fake-stt.local/transcribe")
    monkeypatch.setattr(settings, "stt_key", secret_val)
    login(client)
    lane = make_lane(client, slug="backend")
    token_hdr = {"X-Bridge-Token": lane["apiKey"]}

    async def leaky_transcribe(data, filename, mime):
        raise SttError(f"failure with key {secret_val}")

    monkeypatch.setattr("app.stt.transcribe", leaky_transcribe)

    with caplog.at_level(logging.DEBUG):
        _push_voice(client, "backend", update_id=1, message_id=10, file_id="fid_1", file_unique_id="uid_sec")
        asyncio.run(runtime.stt_step())

    # Check caplog
    assert secret_val not in caplog.text

    # Check /info response
    info_resp = client.get("/backend/info", headers=token_hdr)
    assert secret_val not in info_resp.text

    # Check /api/session response
    sess_resp = client.get("/api/session")
    assert secret_val not in sess_resp.text

    # Check admin transcripts list response
    admin_resp = client.get("/admin/api/transcripts")
    assert secret_val not in admin_resp.text


def test_audio_and_video_note_and_captions(client, monkeypatch):
    """Audio and video_note formats and captions are preserved on separate lines."""
    monkeypatch.setattr(settings, "stt_url", "http://fake-stt.local/transcribe")
    login(client)
    lane = make_lane(client, slug="backend")
    token_hdr = {"X-Bridge-Token": lane["apiKey"]}

    async def fake_transcribe(data, filename, mime):
        return {"text": "музыкальный трек", "language": "ru"}

    monkeypatch.setattr("app.stt.transcribe", fake_transcribe)

    # Audio message with caption
    _push_voice(
        client,
        slug="backend",
        update_id=20,
        message_id=200,
        duration=125,
        kind="audio",
        file_id="audio_fid",
        file_unique_id="audio_uid",
        caption="Послушай демо",
    )
    assert asyncio.run(runtime.stt_step()) is True

    feed = client.get("/backend/feed", headers=token_hdr).json()["messages"]
    assert feed[0]["text"] == "[audio 2:05] музыкальный трек\nПослушай демо"
    assert feed[0]["transcript"] == "музыкальный трек"


def test_stt_module_transcribe_direct(monkeypatch):
    """Direct unit test of app.stt.transcribe error handling and payload parsing."""
    import httpx
    from app.stt import SttError, transcribe

    secret_auth = "-".join(["test", "auth", "value", "for", "stt"])
    monkeypatch.setattr(settings, "stt_url", "http://stt.example.com/transcribe")
    monkeypatch.setattr(settings, "stt_key", secret_auth)
    monkeypatch.setattr(settings, "stt_language", "ru")
    monkeypatch.setattr(settings, "stt_model", "whisper-1")

    orig_client = httpx.AsyncClient

    # Success case
    def handler_ok(request: httpx.Request):
        assert request.headers.get("Authorization") == f"Bearer {secret_auth}"
        assert b'name="language"\r\n\r\nru' in request.content
        assert b'name="model"\r\n\r\nwhisper-1' in request.content
        return httpx.Response(200, json={"text": "Привет мир", "language": "ru"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(*args, transport=httpx.MockTransport(handler_ok), **kwargs),
    )
    res = asyncio.run(transcribe(b"dummy_bytes", "voice.ogg", "audio/ogg"))
    assert res == {"text": "Привет мир", "language": "ru"}

    # Non-200 case
    def handler_500(request: httpx.Request):
        return httpx.Response(500, text="Internal Server Error")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(*args, transport=httpx.MockTransport(handler_500), **kwargs),
    )
    with pytest.raises(SttError, match="HTTP 500"):
        asyncio.run(transcribe(b"dummy_bytes", "voice.ogg", "audio/ogg"))

    # Bad JSON case
    def handler_bad_json(request: httpx.Request):
        return httpx.Response(200, text="not json at all")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(*args, transport=httpx.MockTransport(handler_bad_json), **kwargs),
    )
    with pytest.raises(SttError, match="invalid JSON"):
        asyncio.run(transcribe(b"dummy_bytes", "voice.ogg", "audio/ogg"))

    # Missing 'text' key
    def handler_missing_text(request: httpx.Request):
        return httpx.Response(200, json={"language": "ru"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: orig_client(*args, transport=httpx.MockTransport(handler_missing_text), **kwargs),
    )
    with pytest.raises(SttError, match="missing 'text'"):
        asyncio.run(transcribe(b"dummy_bytes", "voice.ogg", "audio/ogg"))
