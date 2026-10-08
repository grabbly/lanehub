"""Tests for scripts/import_messages.py."""
import json
import sqlite3
from pathlib import Path

from app import db
from scripts.import_messages import main

MINIMAL_SCHEMA = """
CREATE TABLE messages (
    lane_slug TEXT NOT NULL,
    update_id INTEGER NOT NULL,
    message_id INTEGER,
    chat_id INTEGER,
    chat_title TEXT,
    from_user TEXT,
    text TEXT,
    date INTEGER NOT NULL DEFAULT 0,
    is_outgoing INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lane_slug, update_id)
);
"""

FULL_SCHEMA = """
CREATE TABLE messages (
    lane_slug TEXT NOT NULL,
    update_id INTEGER NOT NULL,
    message_id INTEGER,
    chat_id INTEGER,
    chat_title TEXT,
    from_user TEXT,
    text TEXT,
    date INTEGER NOT NULL DEFAULT 0,
    is_outgoing INTEGER NOT NULL DEFAULT 0,
    media TEXT,
    from_id INTEGER,
    from_username TEXT,
    from_is_bot INTEGER,
    seq INTEGER,
    PRIMARY KEY (lane_slug, update_id)
);
"""


def create_old_db(path: Path, schema: str = MINIMAL_SCHEMA) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    conn.commit()
    return conn


def add_target_lane(slug: str, chat_id: str = "-100500") -> dict:
    bt = ":".join(["123", "x" * 35])
    return db.create_lane(slug, f"Title {slug}", bt, f"{slug}_bot", chat_id)


def insert_minimal_message(
    conn: sqlite3.Connection,
    lane_slug: str,
    update_id: int,
    message_id: int = 100,
    chat_id: int = -100500,
    chat_title: str = "Test Chat",
    from_user: str = "alice",
    text: str = "hello",
    date: int = 1_700_000_000,
    is_outgoing: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO messages (lane_slug, update_id, message_id, chat_id, chat_title, from_user, text, date, is_outgoing) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (lane_slug, update_id, message_id, chat_id, chat_title, from_user, text, date, is_outgoing),
    )
    conn.commit()


def test_import_minimal_schema_into_existing_lane(client, tmp_path):
    add_target_lane("ops")
    old_db = tmp_path / "old.db"
    conn = create_old_db(old_db, MINIMAL_SCHEMA)
    insert_minimal_message(conn, "ops", 1, message_id=10, text="first", date=100)
    insert_minimal_message(conn, "ops", 2, message_id=11, text="second", date=200)
    conn.close()

    ret = main([str(old_db)])
    assert ret == 0

    assert db.count_messages("ops") == 2
    msgs = db.query_messages("ops", since=0, limit=10, order="asc")
    assert len(msgs) == 2
    assert msgs[0]["text"] == "first"
    assert msgs[0]["seq"] is not None
    assert msgs[1]["text"] == "second"
    assert msgs[1]["seq"] is not None
    assert msgs[0]["media"] is None
    assert msgs[0]["fromId"] is None


def test_dedup_on_second_run(client, tmp_path, capsys):
    add_target_lane("ops")
    old_db = tmp_path / "old.db"
    conn = create_old_db(old_db, MINIMAL_SCHEMA)
    insert_minimal_message(conn, "ops", 1, message_id=10, text="msg1", date=100)
    insert_minimal_message(conn, "ops", 2, message_id=20, text="msg2", date=200)
    conn.close()

    ret1 = main([str(old_db)])
    assert ret1 == 0
    assert db.count_messages("ops") == 2
    out1 = capsys.readouterr().out
    assert "ops: 2 inserted, 0 already there" in out1

    # Second run should insert 0 and dedup both
    ret2 = main([str(old_db)])
    assert ret2 == 0
    assert db.count_messages("ops") == 2
    out2 = capsys.readouterr().out
    assert "ops: 0 inserted, 2 already there" in out2


def test_since_filter(client, tmp_path):
    add_target_lane("ops")
    old_db = tmp_path / "old.db"
    conn = create_old_db(old_db, MINIMAL_SCHEMA)
    insert_minimal_message(conn, "ops", 1, message_id=10, text="old1", date=100)
    insert_minimal_message(conn, "ops", 2, message_id=20, text="old2", date=200)
    insert_minimal_message(conn, "ops", 3, message_id=30, text="new3", date=300)
    conn.close()

    ret = main([str(old_db), "--since", "200"])
    assert ret == 0

    assert db.count_messages("ops") == 1
    msgs = db.query_messages("ops", since=0, limit=10, order="asc")
    assert len(msgs) == 1
    assert msgs[0]["updateId"] == 3
    assert msgs[0]["text"] == "new3"


def test_lane_mapping(client, tmp_path):
    add_target_lane("target-lane")
    old_db = tmp_path / "old.db"
    conn = create_old_db(old_db, MINIMAL_SCHEMA)
    insert_minimal_message(conn, "source-lane", 1, message_id=10, text="mapped text")
    conn.close()

    ret = main([str(old_db), "--lane", "source-lane=target-lane"])
    assert ret == 0

    assert db.count_messages("target-lane") == 1
    assert db.count_messages("source-lane") == 0
    msgs = db.query_messages("target-lane", since=0, limit=10, order="asc")
    assert msgs[0]["text"] == "mapped text"


def test_lane_filter_only_selected(client, tmp_path):
    add_target_lane("lane-a")
    add_target_lane("lane-b")
    old_db = tmp_path / "old.db"
    conn = create_old_db(old_db, MINIMAL_SCHEMA)
    insert_minimal_message(conn, "lane-a", 1, message_id=10, text="msg a")
    insert_minimal_message(conn, "lane-b", 2, message_id=20, text="msg b")
    conn.close()

    # Specifying --lane lane-a keeps only lane-a
    ret = main([str(old_db), "--lane", "lane-a"])
    assert ret == 0

    assert db.count_messages("lane-a") == 1
    assert db.count_messages("lane-b") == 0


def test_unknown_lane_skipped(client, tmp_path, capsys):
    add_target_lane("known-lane")
    old_db = tmp_path / "old.db"
    conn = create_old_db(old_db, MINIMAL_SCHEMA)
    insert_minimal_message(conn, "known-lane", 1, message_id=10, text="known msg")
    insert_minimal_message(conn, "ghost-lane", 2, message_id=20, text="ghost 1")
    insert_minimal_message(conn, "ghost-lane", 3, message_id=30, text="ghost 2")
    conn.close()

    ret = main([str(old_db)])
    assert ret == 0

    assert db.count_messages("known-lane") == 1
    assert db.count_messages("ghost-lane") == 0
    out = capsys.readouterr().out
    assert "known-lane: 1 inserted, 0 already there" in out
    assert "Total 'no such lane' skipped: 2" in out


def test_dry_run_inserts_nothing(client, tmp_path, capsys):
    add_target_lane("ops")
    old_db = tmp_path / "old.db"
    conn = create_old_db(old_db, MINIMAL_SCHEMA)
    insert_minimal_message(conn, "ops", 1, message_id=10, text="msg")
    conn.close()

    ret = main([str(old_db), "--dry-run"])
    assert ret == 0

    assert db.count_messages("ops") == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "ops: 1 inserted, 0 already there" in out


def test_rows_show_up_in_query_feed(client, tmp_path):
    add_target_lane("backend", chat_id="-100500")
    old_db = tmp_path / "old.db"
    conn = create_old_db(old_db, MINIMAL_SCHEMA)
    insert_minimal_message(conn, "backend", 101, message_id=1, chat_id=-100500, text="feed message 1")
    insert_minimal_message(conn, "backend", 102, message_id=2, chat_id=-100500, text="feed message 2")
    conn.close()

    ret = main([str(old_db)])
    assert ret == 0

    feed = db.query_feed(since_date=0, limit=10, order="asc", chat_id=-100500)
    assert len(feed) == 2
    assert feed[0]["text"] == "feed message 1"
    assert feed[1]["text"] == "feed message 2"
    assert feed[0]["seq"] > 0
    assert feed[1]["seq"] > feed[0]["seq"]


def test_full_current_schema_import(client, tmp_path):
    add_target_lane("backend")
    old_db = tmp_path / "old.db"
    conn = create_old_db(old_db, FULL_SCHEMA)
    media_json = json.dumps({"fileId": "fid12345", "type": "photo"})
    conn.execute(
        "INSERT INTO messages (lane_slug, update_id, message_id, chat_id, chat_title, from_user, text, date, "
        "is_outgoing, media, from_id, from_username, from_is_bot, seq) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("backend", 42, 500, -100500, "Full Chat", "bob", "attached photo", 1_700_000_123, 0,
         media_json, 98765, "bob_tg", 0, 11),
    )
    conn.commit()
    conn.close()

    ret = main([str(old_db)])
    assert ret == 0

    msgs = db.query_messages("backend", since=0, limit=10, order="asc")
    assert len(msgs) == 1
    m = msgs[0]
    assert m["updateId"] == 42
    assert m["text"] == "attached photo"
    assert m["media"] == {"fileId": "fid12345", "type": "photo"}
    assert m["fromId"] == 98765
    assert m["fromUsername"] == "bob_tg"
    assert m["fromIsBot"] is False
    assert m["seq"] is not None


def test_bad_arguments_and_unreadable_db(client, tmp_path):
    valid_db = tmp_path / "valid.db"
    create_old_db(valid_db, MINIMAL_SCHEMA).close()

    # Nonexistent file
    assert main([str(tmp_path / "missing.db")]) == 2

    # Directory instead of file
    assert main([str(tmp_path)]) == 2

    # Corrupt / not a SQLite database
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_text("not a database file")
    assert main([str(corrupt)]) == 2

    # Missing messages table
    empty = tmp_path / "empty.db"
    c = sqlite3.connect(empty)
    c.execute("CREATE TABLE other_table (id INT)")
    c.commit()
    c.close()
    assert main([str(empty)]) == 2

    # Invalid --since
    assert main([str(valid_db), "--since", "not-a-number"]) == 2

    # Invalid --lane
    assert main([str(valid_db), "--lane", "invalid="]) == 2
