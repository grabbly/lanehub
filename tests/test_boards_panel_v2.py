"""Tests for the client panel updates (v0.11.0):
- Idempotent schema migration & upgrade path from old DB
- Manual task archive / unarchive (agent & admin), listing filters (?archived=1|all), ledger counts
- Status line «Де ми зараз» (agent PUT, admin PATCH, validation, fallback)
- Client data.json whitelist, status, updatedAt, and monthly archive grouping
- Operator panel HTML entry points
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import pytest
from app.boards import store
from tests.conftest import login, make_lane


def test_upgrade_path_from_old_schema(tmp_path, monkeypatch):
    """An old-shape DB without the new columns opens and works idempotently."""
    db_file = tmp_path / "old_hub.db"
    conn = sqlite3.connect(str(db_file))

    # Create old tables without status_i18n, status_updated_at, archived_at
    conn.executescript("""
    CREATE TABLE boards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL DEFAULT '',
        team_lang TEXT NOT NULL DEFAULT 'en',
        client_lang TEXT NOT NULL DEFAULT 'en',
        currency TEXT NOT NULL DEFAULT 'zł',
        rate REAL NULL,
        client_token_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );

    CREATE TABLE board_tasks (
        id TEXT PRIMARY KEY,
        board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'backlog',
        position REAL NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 1,
        title_i18n TEXT NOT NULL DEFAULT '{}',
        client_note_i18n TEXT NOT NULL DEFAULT '{}',
        internal_note TEXT NOT NULL DEFAULT '',
        grp TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        source_message_id INTEGER NULL,
        pay TEXT NOT NULL DEFAULT 'estimate',
        price REAL NOT NULL DEFAULT 0,
        hours REAL NULL,
        client_visible INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        updated_by TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE board_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
        at INTEGER NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        task_id TEXT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}'
    );
    """)

    # Insert an old board and task
    dummy_hash = "abc" * 10
    conn.execute(
        "INSERT INTO boards (chat_id, slug, title, client_token_hash, created_at, updated_at) "
        "VALUES ('-100123', 'oldboard', 'Old Board', ?, 1700000000, 1700000000)",
        (dummy_hash,),
    )
    conn.execute(
        "INSERT INTO board_tasks (id, board_id, status, position, version, title_i18n, created_at, updated_at) "
        "VALUES ('t-old', 1, 'done', 10, 1, '{\"en\":\"Old Task\"}', 1700000000, 1700000000)",
    )
    conn.commit()
    conn.close()

    # Point store to this old db and open it via store
    from app.config import settings

    monkeypatch.setattr(settings, "db_path", db_file)
    with store._conn():
        pass

    # Verify new columns exist
    with sqlite3.connect(str(db_file)) as c:
        b_cols = [r[1] for r in c.execute("PRAGMA table_info(boards)").fetchall()]
        assert "status_i18n" in b_cols
        assert "status_updated_at" in b_cols

        t_cols = [r[1] for r in c.execute("PRAGMA table_info(board_tasks)").fetchall()]
        assert "archived_at" in t_cols

    # Verify old data is accessible and has defaults
    b = store.get_board(1)
    assert b is not None
    assert b["statusI18n"] == {}
    assert b["statusUpdatedAt"] is None

    tasks = store.list_tasks(1)
    assert len(tasks) == 1
    assert tasks[0]["id"] == "t-old"
    assert tasks[0]["archivedAt"] is None

    # Archive old task and update status
    store.update_task(1, "t-old", archived=True, actor="admin")
    store.set_board_status(1, {"en": "Upgraded"}, actor="admin")

    b_after = store.get_board(1)
    assert b_after["statusI18n"] == {"en": "Upgraded"}
    assert b_after["statusUpdatedAt"] is not None

    archived_tasks = store.list_tasks(1, archived="1")
    assert len(archived_tasks) == 1
    assert archived_tasks[0]["archivedAt"] is not None


def test_archive_unarchive_agent_and_admin(boards_client):
    """Test manual archive/unarchive via agent and admin, listing filters, and ledger counting."""
    login(boards_client)
    lane_info = make_lane(boards_client, slug="archivelane", chat_id="-100888")
    bridge_header = {"X-Bridge-Token": lane_info["apiKey"]}

    # Create board via admin
    b_res = boards_client.post(
        "/admin/api/boards",
        json={
            "chatId": "-100888",
            "slug": "archive-board",
            "title": "Archive Board",
            "teamLang": "en",
            "clientLang": "uk",
            "currency": "zł",
        },
    )
    assert b_res.status_code == 201, b_res.text
    board_id = b_res.json()["id"]

    # Add 2 tasks: task 1 (deposit, 150 zł), task 2 (paid, 50 zł)
    t1_res = boards_client.post(
        f"/admin/api/boards/{board_id}/tasks",
        json={
            "titleI18n": {"en": "Deposit Task"},
            "status": "done",
            "pay": "deposit",
            "price": 150.0,
        },
    )
    assert t1_res.status_code == 201
    t1 = t1_res.json()

    t2_res = boards_client.post(
        f"/admin/api/boards/{board_id}/tasks",
        json={
            "titleI18n": {"en": "Paid Task"},
            "status": "queue",
            "pay": "paid",
            "price": 50.0,
        },
    )
    assert t2_res.status_code == 201
    t2 = t2_res.json()

    # Check ledger allocated before archive
    board_data = boards_client.get(f"/admin/api/boards/{board_id}").json()
    assert board_data["ledger"]["allocated"] == 150.0
    assert board_data["archivedCount"] == 0

    # 1. Admin archives task 1
    patch1 = boards_client.patch(
        f"/admin/api/boards/{board_id}/tasks/{t1['id']}",
        json={"archived": True, "version": t1["version"]},
    )
    assert patch1.status_code == 200
    assert patch1.json()["archivedAt"] is not None

    # Check event recorded
    events = boards_client.get(f"/admin/api/boards/{board_id}/events").json()
    archived_events = [e for e in events if e["action"] == "archived"]
    assert len(archived_events) == 1
    assert archived_events[0]["taskId"] == t1["id"]

    # Check default task listing (non-archived only)
    admin_tasks = boards_client.get(f"/admin/api/boards/{board_id}/tasks").json()
    assert len(admin_tasks) == 1
    assert admin_tasks[0]["id"] == t2["id"]

    agent_board = boards_client.get("/archivelane/board", headers=bridge_header).json()
    assert len(agent_board["tasks"]) == 1
    assert agent_board["tasks"][0]["id"] == t2["id"]

    # Check ?archived=1
    admin_arch = boards_client.get(f"/admin/api/boards/{board_id}/tasks?archived=1").json()
    assert len(admin_arch) == 1
    assert admin_arch[0]["id"] == t1["id"]

    agent_arch = boards_client.get("/archivelane/board?archived=1", headers=bridge_header).json()
    assert len(agent_arch["tasks"]) == 1
    assert agent_arch["tasks"][0]["id"] == t1["id"]

    # Check ?archived=all
    admin_all = boards_client.get(f"/admin/api/boards/{board_id}/tasks?archived=all").json()
    assert len(admin_all) == 2

    agent_all = boards_client.get("/archivelane/board?archived=all", headers=bridge_header).json()
    assert len(agent_all["tasks"]) == 2

    # Check that ledger allocated still counts archived deposit tasks!
    board_after = boards_client.get(f"/admin/api/boards/{board_id}").json()
    assert board_after["ledger"]["allocated"] == 150.0
    assert board_after["archivedCount"] == 1
    assert agent_board["ledger"]["allocated"] == 150.0

    # 2. Agent unarchives task 1
    agent_unarch = boards_client.patch(
        f"/archivelane/board/tasks/{t1['id']}",
        headers=bridge_header,
        json={"archived": False, "version": patch1.json()["version"]},
    )
    assert agent_unarch.status_code == 200
    assert agent_unarch.json()["archivedAt"] is None

    events2 = boards_client.get(f"/admin/api/boards/{board_id}/events").json()
    unarchived_events = [e for e in events2 if e["action"] == "unarchived"]
    assert len(unarchived_events) == 1
    assert unarchived_events[0]["actor"] == "archivelane"

    # Default task listing now has both
    admin_tasks2 = boards_client.get(f"/admin/api/boards/{board_id}/tasks").json()
    assert len(admin_tasks2) == 2


def test_status_line_endpoints_and_validation(boards_client):
    """Test agent PUT /{lane}/board/status, admin PATCH, validation, and language fallback."""
    login(boards_client)
    lane1 = make_lane(boards_client, slug="botlane1", chat_id="-100111")
    lane2 = make_lane(boards_client, slug="botlane2", chat_id="-100222")

    b_res = boards_client.post(
        "/admin/api/boards",
        json={
            "chatId": "-100111",
            "slug": "status-board",
            "title": "Status Board",
            "teamLang": "en",
            "clientLang": "uk",
        },
    )
    assert b_res.status_code == 201
    board_id = b_res.json()["id"]

    hdr1 = {"X-Bridge-Token": lane1["apiKey"]}
    hdr2 = {"X-Bridge-Token": lane2["apiKey"]}

    # 1. Agent PUT from other chat returns 404
    r_other = boards_client.put(
        "/botlane2/board/status",
        headers=hdr2,
        json={"statusI18n": {"uk": "Тест"}},
    )
    assert r_other.status_code == 404

    # 2. Agent PUT with unknown language code returns 422
    r_bad_lang = boards_client.put(
        "/botlane1/board/status",
        headers=hdr1,
        json={"statusI18n": {"de": "Guten Tag"}},
    )
    assert r_bad_lang.status_code == 422

    # 3. Agent PUT with text > 300 chars returns 422
    r_too_long = boards_client.put(
        "/botlane1/board/status",
        headers=hdr1,
        json={"statusI18n": {"uk": "А" * 301}},
    )
    assert r_too_long.status_code == 422

    # 4. Agent PUT valid status
    r_ok = boards_client.put(
        "/botlane1/board/status",
        headers=hdr1,
        json={"statusI18n": {"uk": "Налаштовуємо сервери", "en": "Configuring servers"}},
    )
    assert r_ok.status_code == 200
    data = r_ok.json()
    assert data["statusI18n"]["uk"] == "Налаштовуємо сервери"
    assert data["statusUpdatedAt"] is not None

    events = boards_client.get(f"/admin/api/boards/{board_id}/events").json()
    status_events = [e for e in events if e["action"] == "status"]
    assert len(status_events) == 1
    assert status_events[0]["actor"] == "botlane1"

    # 5. Empty map clears status
    r_clear = boards_client.put(
        "/botlane1/board/status",
        headers=hdr1,
        json={"statusI18n": {}},
    )
    assert r_clear.status_code == 200
    data_cleared = r_clear.json()
    assert data_cleared["statusI18n"] == {}
    assert data_cleared["statusUpdatedAt"] is None

    # 6. Admin PATCH sets status
    r_admin = boards_client.patch(
        f"/admin/api/boards/{board_id}",
        json={"statusI18n": {"pl": "Gotowe do testów"}},
    )
    assert r_admin.status_code == 200
    assert r_admin.json()["statusI18n"]["pl"] == "Gotowe do testów"
    assert r_admin.json()["statusUpdatedAt"] is not None


def test_client_data_json_structure_and_privacy(boards_client):
    """Test data.json whitelist, status fallback, updatedAt, and monthly archive grouping."""
    login(boards_client)
    make_lane(boards_client, slug="clientlane", chat_id="-100333")

    b_res = boards_client.post(
        "/admin/api/boards",
        json={
            "chatId": "-100333",
            "slug": "client-board",
            "title": "Client Board",
            "teamLang": "en",
            "clientLang": "uk",
            "currency": "zł",
        },
    )
    assert b_res.status_code == 201
    b_data = b_res.json()
    board_id = b_data["id"]
    client_path = b_data["clientPath"]  # /client-board/b/<token>/

    hidden_note_1 = "hidden" + "_internal_note_active_999"
    hidden_note_2 = "hidden" + "_archive_note_888"

    # Set status
    boards_client.patch(
        f"/admin/api/boards/{board_id}",
        json={"statusI18n": {"uk": "Статус для клієнта", "en": "Client status"}},
    )

    # 1. Active task (client-visible, in waiting)
    boards_client.post(
        f"/admin/api/boards/{board_id}/tasks",
        json={
            "titleI18n": {"uk": "Чекаємо доступу", "en": "Waiting for access"},
            "clientNoteI18n": {"uk": "Надайте API ключ"},
            "internalNote": hidden_note_1,
            "status": "waiting",
            "pay": "deposit",
            "price": 80.0,
            "grp": "Core",
            "hours": 2.0,
            "source": "#101",
            "clientVisible": True,
        },
    )

    # 2. Archived task in October 2026 (deposit, 120 zł)
    t_oct1 = boards_client.post(
        f"/admin/api/boards/{board_id}/tasks",
        json={
            "titleI18n": {"uk": "Жовтень Завдання 1"},
            "status": "done",
            "pay": "deposit",
            "price": 120.0,
            "clientVisible": True,
        },
    ).json()

    # 3. Archived task in October 2026 (paid, 60 zł)
    t_oct2 = boards_client.post(
        f"/admin/api/boards/{board_id}/tasks",
        json={
            "titleI18n": {"uk": "Жовтень Завдання 2"},
            "status": "done",
            "pay": "paid",
            "price": 60.0,
            "clientVisible": True,
        },
    ).json()

    # 4. Archived task in September 2026 (estimate, 40 zł)
    t_sep = boards_client.post(
        f"/admin/api/boards/{board_id}/tasks",
        json={
            "titleI18n": {"uk": "Вересень Завдання"},
            "status": "done",
            "pay": "estimate",
            "price": 40.0,
            "clientVisible": True,
        },
    ).json()

    # 5. Archived task that is hidden from client (clientVisible=False)
    t_hidden = boards_client.post(
        f"/admin/api/boards/{board_id}/tasks",
        json={
            "titleI18n": {"uk": "Приховане завдання"},
            "internalNote": hidden_note_2,
            "status": "done",
            "pay": "paid",
            "price": 99.0,
            "clientVisible": False,
        },
    ).json()

    # Archive them via PATCH
    boards_client.patch(f"/admin/api/boards/{board_id}/tasks/{t_oct1['id']}", json={"archived": True, "version": t_oct1["version"]})
    boards_client.patch(f"/admin/api/boards/{board_id}/tasks/{t_oct2['id']}", json={"archived": True, "version": t_oct2["version"]})
    boards_client.patch(f"/admin/api/boards/{board_id}/tasks/{t_sep['id']}", json={"archived": True, "version": t_sep["version"]})
    boards_client.patch(f"/admin/api/boards/{board_id}/tasks/{t_hidden['id']}", json={"archived": True, "version": t_hidden["version"]})

    # Adjust timestamps in DB for grouping test: October 2026 (1792000000) and September 2026 (1789000000)
    ts_oct = int(datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    ts_sep = int(datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc).timestamp())

    from app.config import settings

    with sqlite3.connect(str(settings.db_path)) as conn:
        conn.execute("UPDATE board_tasks SET archived_at = ? WHERE id IN (?, ?)", (ts_oct, t_oct1["id"], t_oct2["id"]))
        conn.execute("UPDATE board_tasks SET archived_at = ? WHERE id = ?", (ts_sep, t_sep["id"]))
        conn.execute("UPDATE board_tasks SET archived_at = ? WHERE id = ?", (ts_oct, t_hidden["id"]))
        conn.commit()

    # Fetch data.json
    res_data = boards_client.get(f"{client_path}data.json?lang=uk")
    assert res_data.status_code == 200
    d = res_data.json()

    # Top-level keys check
    expected_top_keys = {
        "title",
        "currency",
        "clientLang",
        "langs",
        "updatedAt",
        "status",
        "statusUpdatedAt",
        "ledger",
        "ui",
        "tasks",
        "archive",
    }
    assert set(d.keys()) == expected_top_keys

    # Status check with resolution
    assert d["status"] == "Статус для клієнта"
    assert d["statusUpdatedAt"] is not None

    # Fallback status resolution test: requesting pl falls back to clientLang (uk)
    res_pl = boards_client.get(f"{client_path}data.json?lang=pl")
    assert res_pl.status_code == 200
    assert res_pl.json()["status"] == "Статус для клієнта"

    # Tasks check: non-archived only, with updatedAt
    assert len(d["tasks"]) == 1
    task = d["tasks"][0]
    expected_task_keys = {"title", "note", "status", "pay", "price", "position", "updatedAt", "id", "clientDoneAt"}
    assert set(task.keys()) == expected_task_keys
    assert task["id"] is not None
    assert task["clientDoneAt"] is None
    assert task["title"] == "Чекаємо доступу"
    assert task["note"] == "Надайте API ключ"
    assert isinstance(task["updatedAt"], int)

    # Archive check: grouped by month, newest first
    archive = d["archive"]
    assert len(archive) == 2

    # Month 1: 2026-10
    assert archive[0]["month"] == "2026-10"
    assert archive[0]["count"] == 2  # hidden task is NOT counted
    assert archive[0]["sum"] == 180.0  # 120 + 60
    assert len(archive[0]["items"]) == 2
    for item in archive[0]["items"]:
        assert set(item.keys()) == {"title", "price", "pay"}

    # Month 2: 2026-09
    assert archive[1]["month"] == "2026-09"
    assert archive[1]["count"] == 1
    assert archive[1]["sum"] == 0.0  # pay=estimate is not counted in sum
    assert len(archive[1]["items"]) == 1
    assert set(archive[1]["items"][0].keys()) == {"title", "price", "pay"}

    # Privacy verification on raw text: no sensitive or hidden details appear
    raw_body = res_data.text
    assert hidden_note_1 not in raw_body
    assert hidden_note_2 not in raw_body
    assert "Приховане завдання" not in raw_body
    assert "Core" not in raw_body
    assert "#101" not in raw_body
    assert "internalNote" not in raw_body
    assert "source" not in raw_body


def test_operator_panel_entry_points(client):
    """GET / operator panel HTML contains the new status line editor and archive toggle."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Status line markup
    assert 'id="bd-status-wrap"' in html
    assert 'id="bd-status-view"' in html
    assert 'id="bd-status-time"' in html
    assert 'id="bd-btn-edit-status"' in html
    assert 'id="bd-status-dlg"' in html
    assert 'id="bd-status-form"' in html
    assert 'id="bd-status-lang-strip"' in html

    # Archive controls markup
    assert 'id="bd-btn-toggle-archive"' in html
    assert 'id="bd-archive-view"' in html
    assert 'id="bd-archive-months"' in html
    assert 'id="bd-cd-archive"' in html

    # JS function presence
    assert "function openStatusDialog" in html
    assert "function selectStatusDialogLang" in html
    assert "function archiveTask" in html
    assert "function renderArchiveView" in html
