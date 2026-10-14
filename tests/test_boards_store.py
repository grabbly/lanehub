import pytest
from app.boards import store
from app.config import settings


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "test_boards.db")


def test_board_lifecycle_and_token():
    board, raw_token = store.create_board(
        chat_id="-100123456",
        slug="test-board",
        title="Test Board",
        team_lang="ru",
        client_lang="uk",
        currency="zł",
        rate=40.0,
    )
    assert board["slug"] == "test-board"
    assert board["title"] == "Test Board"
    assert board["teamLang"] == "ru"
    assert board["clientLang"] == "uk"
    assert board["chatId"] == "-100123456"
    assert "clientToken" not in board
    assert "clientTokenHash" not in board

    stored_hash = store.get_board_token_hash(board["id"])
    assert stored_hash is not None
    assert store.verify_client_token(raw_token, stored_hash) is True
    assert store.verify_client_token("wrong-token", stored_hash) is False

    # Rotate token
    new_token = store.rotate_client_token(board["id"])
    assert new_token != raw_token
    new_hash = store.get_board_token_hash(board["id"])
    assert store.verify_client_token(new_token, new_hash) is True
    assert store.verify_client_token(raw_token, new_hash) is False

    # Update board
    updated = store.update_board(board["id"], title="Updated Title", currency="USD")
    assert updated["title"] == "Updated Title"
    assert updated["currency"] == "USD"

    # Reserved slug
    with pytest.raises(ValueError):
        store.create_board(chat_id="-100", slug="admin")

    # Invalid slug
    with pytest.raises(ValueError):
        store.create_board(chat_id="-100", slug="Invalid Slug!")


def test_tasks_crud_positions_and_version():
    board, _ = store.create_board(chat_id="-100", slug="task-board")
    board_id = board["id"]

    # Create task 1 in backlog
    t1 = store.create_task(
        board_id=board_id,
        title_i18n={"ru": "Задача 1", "uk": "Задача 1 укр"},
        status="backlog",
        pay="deposit",
        price=100.0,
    )
    assert t1["position"] == 10.0
    assert t1["version"] == 1
    assert t1["pay"] == "deposit"
    assert t1["price"] == 100.0
    assert "en" in t1["missingLangs"]
    assert "pl" in t1["missingLangs"]
    assert "ru" not in t1["missingLangs"]

    # Create task 2 in backlog -> auto-position +10
    t2 = store.create_task(
        board_id=board_id,
        title_i18n={"ru": "Задача 2"},
        status="backlog",
        pay="paid",
        price=50.0,
    )
    assert t2["position"] == 20.0

    # Create task 3 in doing -> first in doing -> 10.0
    t3 = store.create_task(
        board_id=board_id,
        title_i18n={"en": "Task 3"},
        status="doing",
    )
    assert t3["position"] == 10.0

    # Update task 1 without version
    t1_updated = store.update_task(
        board_id=board_id,
        task_id=t1["id"],
        price=120.0,
        status="queue",
    )
    assert t1_updated["version"] == 2
    assert t1_updated["price"] == 120.0
    assert t1_updated["status"] == "queue"

    # Update task 1 with wrong version -> VersionConflictError
    with pytest.raises(store.VersionConflictError) as exc_info:
        store.update_task(
            board_id=board_id,
            task_id=t1["id"],
            price=150.0,
            version=1,  # current is 2
        )
    assert exc_info.value.current_task["version"] == 2

    # Update task 1 with correct version
    t1_updated2 = store.update_task(
        board_id=board_id,
        task_id=t1["id"],
        price=150.0,
        version=2,
    )
    assert t1_updated2["version"] == 3
    assert t1_updated2["price"] == 150.0

    # Delete task
    assert store.delete_task(board_id, t3["id"]) is True
    assert store.get_task(t3["id"]) is None


def test_derived_ledger_and_columns_summary():
    board, _ = store.create_board(chat_id="-100", slug="ledger-board")
    bid = board["id"]

    # Initially empty
    derived = store.derive_ledger(bid)
    assert derived == {"paid": 0.0, "deposit": 0.0, "allocated": 0.0, "remaining": 0.0}

    # Add payments and deposits
    store.create_ledger_entry(bid, kind="payment", amount=550.0, at="2026-10-13", note="stage 1")
    store.create_ledger_entry(bid, kind="deposit", amount=450.0, at="2026-10-13", note="stage 2 deposit")
    store.create_ledger_entry(bid, kind="refund", amount=50.0, at="2026-10-14", note="refund partial")

    # Add tasks
    # Deposit task 1: 120
    store.create_task(bid, title_i18n={"en": "T1"}, status="doing", pay="deposit", price=120.0)
    # Deposit task 2: 80
    store.create_task(bid, title_i18n={"en": "T2"}, status="queue", pay="deposit", price=80.0)
    # Paid task: 550 (should not count towards allocated)
    store.create_task(bid, title_i18n={"en": "T3"}, status="done", pay="paid", price=550.0)
    # Estimate task: 200 (should not count towards allocated)
    store.create_task(bid, title_i18n={"en": "T4"}, status="backlog", pay="estimate", price=200.0)

    derived = store.derive_ledger(bid)
    assert derived["paid"] == 550.0
    # Deposit: 450 - 50 = 400
    assert derived["deposit"] == 400.0
    # Allocated: 120 + 80 = 200
    assert derived["allocated"] == 200.0
    # Remaining: 400 - 200 = 200
    assert derived["remaining"] == 200.0

    cols = store.get_columns_summary(bid)
    assert cols["doing"]["count"] == 1
    assert cols["doing"]["sum"] == 120.0
    assert cols["queue"]["count"] == 1
    assert cols["queue"]["sum"] == 80.0
    assert cols["done"]["count"] == 1
    assert cols["done"]["sum"] == 550.0
    assert cols["backlog"]["count"] == 1
    assert cols["backlog"]["sum"] == 0.0  # estimate doesn't contribute to column sum
    assert cols["waiting"]["count"] == 0
    assert cols["waiting"]["sum"] == 0.0


def test_i18n_text_resolution():
    mapping = {"ru": "Текст RU", "uk": "Текст UK"}
    # Viewer uk -> UK
    assert store.resolve_text(mapping, "uk", "ru") == "Текст UK"
    # Viewer ru -> RU
    assert store.resolve_text(mapping, "ru", "uk") == "Текст RU"
    # Viewer en -> fallback to team_lang (ru)
    assert store.resolve_text(mapping, "en", "ru") == "Текст RU"
    # Viewer pl -> team_lang (en, empty) -> first non-empty
    assert store.resolve_text(mapping, "pl", "en") in ("Текст RU", "Текст UK")
    # Empty map
    assert store.resolve_text({}, "en", "ru") == ""


def test_events_and_cascade_delete():
    board, _ = store.create_board(chat_id="-100", slug="cascade-board")
    bid = board["id"]
    t = store.create_task(bid, title_i18n={"en": "Task"}, actor="agent-lane")
    l = store.create_ledger_entry(bid, kind="payment", amount=100.0, at="2026-10-13", actor="admin")

    events = store.list_events(bid)
    actions = [e["action"] for e in events]
    assert "board_created" in actions
    assert "task_created" in actions
    assert "ledger_created" in actions

    # None of the events should leak any tokens
    for e in events:
        payload_str = str(e["payload"])
        assert "clientToken" not in payload_str
        assert "token_hash" not in payload_str

    # Cascade delete
    assert store.delete_board(bid) is True
    assert store.get_board(bid) is None
    assert store.get_task(t["id"]) is None
    assert store.get_ledger_entry(l["id"]) is None
    assert len(store.list_events(bid)) == 0
