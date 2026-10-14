import json
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from app.boards import store
from app.config import settings


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    test_db = tmp_path / "import_test.db"
    monkeypatch.setattr(settings, "db_path", test_db)


def test_import_store_logic(tmp_path):
    slug = "import-slug"

    tasks_raw = [
        {
            "id": "task-1",
            "title": "Title RU 1",
            "title_uk": "Title UK 1",
            "note": "Internal note 1",
            "note_uk": "Client note 1",
            "client": True,
            "order": 1.0,
            "group": "Group A",
            "source": "#101",
            "status": "queue",
            "pay": "warranty",  # should map to rework
            "price": "120,50",  # comma string -> 120.5
            "hours": "1,25",  # comma string -> 1.25
        },
        {
            "id": "task-2",
            "title": "Title RU 2",
            "note": "Existing note",
            "client": False,  # client_visible = False
            "order": 2.0,
            "status": "unknown-status",  # maps to backlog + warning
            "pay": "unknown-pay",  # maps to estimate + warning
            "price": 50,
            "hours": "0,5–1",  # range -> hours None + note appended + warning
        },
    ]

    custom_token = secrets.token_urlsafe(16)

    # Dry run test first
    dry_res = store.import_board(
        board_slug=slug,
        tasks_data=tasks_raw,
        ledger_entries=[("payment", 550.0, "2026-10-13", "Dry run ledger")],
        dry_run=True,
        create_params={"chat_id": "-100777", "title": "Import Board"},
        client_token=custom_token,
    )
    assert dry_res["dryRun"] is True
    assert dry_res["createdTasks"] == 2
    assert len(dry_res["warnings"]) >= 3
    # Board should NOT have been created in DB
    assert store.get_board_by_slug(slug) is None

    # Real import
    res = store.import_board(
        board_slug=slug,
        tasks_data=tasks_raw,
        ledger_entries=[
            ("payment", 550.0, "2026-10-13", "Initial payment"),
            ("deposit", 450.0, "2026-10-13", "Deposit"),
        ],
        dry_run=False,
        create_params={"chat_id": "-100777", "title": "Import Board"},
        client_token=custom_token,
    )
    assert res["createdTasks"] == 2
    assert res["createdLedger"] == 2

    # Verify board created and token works
    b = store.get_board_by_slug(slug)
    assert b is not None
    assert b["title"] == "Import Board"
    assert b["chatId"] == "-100777"
    stored_hash = store.get_board_token_hash(b["id"])
    assert store.verify_client_token(custom_token, stored_hash) is True

    # Verify task 1 mapping
    t1 = store.get_task("task-1")
    assert t1 is not None
    assert t1["titleI18n"]["ru"] == "Title RU 1"
    assert t1["titleI18n"]["uk"] == "Title UK 1"
    assert t1["internalNote"] == "Internal note 1"
    assert t1["clientNoteI18n"]["uk"] == "Client note 1"
    assert t1["clientVisible"] is True
    assert t1["pay"] == "rework"  # warranty -> rework
    assert t1["price"] == 120.5
    assert t1["hours"] == 1.25
    assert t1["grp"] == "Group A"
    assert t1["source"] == "#101"
    assert t1["status"] == "queue"

    # Verify task 2 mapping
    t2 = store.get_task("task-2")
    assert t2 is not None
    assert t2["clientVisible"] is False
    assert t2["status"] == "backlog"  # unknown -> backlog
    assert t2["pay"] == "estimate"  # unknown -> estimate
    assert t2["hours"] is None
    assert "hours: 0,5–1" in t2["internalNote"]

    # Verify ledger entries
    ledger = store.list_ledger(b["id"])
    assert len(ledger) == 2
    derived = store.derive_ledger(b["id"])
    assert derived["paid"] == 550.0
    assert derived["deposit"] == 450.0

    # Idempotent re-import: modify task 1 in source and re-import
    tasks_raw[0]["price"] = "130"
    re_res = store.import_board(
        board_slug=slug,
        tasks_data=tasks_raw,
        dry_run=False,
    )
    assert re_res["createdTasks"] == 0
    assert re_res["updatedTasks"] == 2

    t1_updated = store.get_task("task-1")
    assert t1_updated["price"] == 130.0
    assert t1_updated["version"] == 2


def test_import_cli(tmp_path):
    db_file = tmp_path / "cli_test.db"
    export_dir = tmp_path / "export"
    export_dir.mkdir()

    task_file = export_dir / "t1.json"
    task_file.write_text(
        json.dumps({
            "id": "cli-task-1",
            "title": "CLI Task",
            "status": "queue",
            "pay": "deposit",
            "price": 100,
        }),
        encoding="utf-8",
    )

    token = secrets.token_urlsafe(16)
    cmd = [
        sys.executable,
        "scripts/import_board.py",
        "--db",
        str(db_file),
        "--board",
        "cli-board",
        "--create",
        "--chat-id",
        "-100888",
        "--title",
        "CLI Board",
        "--ledger",
        "payment:550:2026-10-13:cli note",
        "--client-token-stdin",
        str(export_dir),
    ]

    p = subprocess.run(
        cmd,
        input=token.encode("utf-8"),
        capture_output=True,
        check=True,
    )
    out = p.stdout.decode("utf-8")
    assert "Created tasks: 1" in out
    assert "Created ledger entries: 1" in out
    # Rule check: never print the token!
    assert token not in out

    # Verify directly via store pointing to this db
    settings.db_path = db_file
    b = store.get_board_by_slug("cli-board")
    assert b is not None
    assert b["title"] == "CLI Board"
    stored_hash = store.get_board_token_hash(b["id"])
    assert store.verify_client_token(token, stored_hash) is True


def test_import_refuses_task_id_of_another_board(boards_client):
    from app.boards import store
    import pytest

    store.create_board(chat_id="-100500", slug="first", title="First")
    store.create_board(chat_id="-100501", slug="second", title="Second")
    store.import_board("first", [{"id": "shared-id", "title": "One"}])
    with pytest.raises(ValueError, match="another board"):
        store.import_board("second", [{"id": "shared-id", "title": "Two"}])
    assert store.get_task("shared-id")["titleI18n"]["ru"] == "One"


def test_import_cli_takes_id_from_file_name(tmp_path):
    # Artifact exports are bare documents named <doc_id>.json.
    export = tmp_path / "tasks"
    export.mkdir()
    (export / "auto-lang.json").write_text(
        json.dumps({"title": "Задача", "title_uk": "Завдання", "hours": "1,25"}), encoding="utf-8"
    )
    db_path = tmp_path / "cli.db"
    script = Path(__file__).resolve().parents[1] / "scripts" / "import_board.py"
    res = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--board", "filename-ids", "--create",
         "--chat-id", "-100500", str(export)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    settings.db_path = db_path
    task = store.get_task("auto-lang")
    assert task is not None and task["hours"] == 1.25


def test_import_skips_hidden_files(tmp_path):
    # A tar made on macOS carries AppleDouble files (._name.json) that are not JSON.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from import_board import load_tasks_from_path

    (tmp_path / "card.json").write_text(json.dumps({"title": "Задача"}), encoding="utf-8")
    (tmp_path / "._card.json").write_bytes(b"\x00\x05\x16\x07\xa3binary")
    tasks = load_tasks_from_path(tmp_path)
    assert [t["id"] for t in tasks] == ["card"]
