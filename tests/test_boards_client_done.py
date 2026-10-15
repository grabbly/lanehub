import sqlite3
import time
from datetime import datetime, timezone
import pytest
from app.boards import store
from app.config import settings
from tests.conftest import login, make_lane


def test_upgrade_path_old_db_without_client_done_column(tmp_path, monkeypatch):
    """Test old database without client_done_at column is migrated cleanly."""
    db_file = tmp_path / "old_board_upgrade.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript("""
    CREATE TABLE boards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL DEFAULT '',
        team_lang TEXT NOT NULL DEFAULT 'en',
        client_lang TEXT NOT NULL DEFAULT 'en',
        currency TEXT NOT NULL DEFAULT 'zł',
        rate REAL,
        status_i18n TEXT NOT NULL DEFAULT '{}',
        status_updated_at INTEGER,
        client_token_hash TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE board_tasks (
        id TEXT PRIMARY KEY,
        board_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        position REAL NOT NULL DEFAULT 0,
        grp TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        source_message_id INTEGER,
        pay TEXT NOT NULL DEFAULT 'estimate',
        price REAL NOT NULL DEFAULT 0,
        hours REAL,
        client_visible INTEGER NOT NULL DEFAULT 1,
        title_i18n TEXT NOT NULL DEFAULT '{}',
        client_note_i18n TEXT NOT NULL DEFAULT '{}',
        internal_note TEXT NOT NULL DEFAULT '',
        archived_at INTEGER,
        version INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        updated_by TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE board_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        board_id INTEGER NOT NULL,
        task_id TEXT,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        at INTEGER NOT NULL
    );
    """)

    dummy_h = "hash" + "dummy_token_999"
    conn.execute(
        "INSERT INTO boards (chat_id, slug, title, client_token_hash, created_at, updated_at) "
        "VALUES ('-100555', 'upgradeboard', 'Upgrade Board', ?, 1700000000, 1700000000)",
        (dummy_h,),
    )
    conn.execute(
        "INSERT INTO board_tasks (id, board_id, status, position, version, title_i18n, created_at, updated_at) "
        "VALUES ('t-upgrade', 1, 'waiting', 10, 1, '{\"en\":\"Upgrade Task\"}', 1700000000, 1700000000)",
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "db_path", db_file)
    with store._conn():
        pass

    with sqlite3.connect(str(db_file)) as c:
        t_cols = [r[1] for r in c.execute("PRAGMA table_info(board_tasks)").fetchall()]
        assert "client_done_at" in t_cols

    task = store.get_task("t-upgrade")
    assert task is not None
    assert task["clientDoneAt"] is None

    # Update with client_done=True works and persists
    updated = store.update_task(1, "t-upgrade", client_done=True, actor="admin")
    assert updated["clientDoneAt"] is not None


def test_client_done_tick_lifecycle_and_security(boards_client):
    """Test client done endpoint: done/undone, 404s, 422 extra fields, 429 rate limit."""
    c = boards_client
    slug = "tick-board"
    board, raw_token = store.create_board(
        chat_id="-100777",
        slug=slug,
        title="Tick Board",
        team_lang="en",
        client_lang="uk",
    )
    board_id = board["id"]

    # Board 2 for cross-board testing
    b2, _ = store.create_board(
        chat_id="-100888",
        slug="tick-board-two",
        title="Tick Board Two",
    )
    t2 = store.create_task(
        b2["id"],
        title_i18n={"en": "Board 2 task"},
        status="waiting",
    )

    t_waiting = store.create_task(
        board_id,
        title_i18n={"en": "Waiting task"},
        status="waiting",
        client_visible=True,
    )
    t_id = t_waiting["id"]

    t_hidden = store.create_task(
        board_id,
        title_i18n={"en": "Hidden waiting task"},
        status="waiting",
        client_visible=False,
    )

    t_doing = store.create_task(
        board_id,
        title_i18n={"en": "Doing task"},
        status="doing",
        client_visible=True,
    )

    # 1. Done tick: done -> True
    resp = c.post(f"/{slug}/b/{raw_token}/tasks/{t_id}/done", json={"done": True})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert resp.headers["referrer-policy"] == "no-referrer"
    data = resp.json()
    assert data["id"] == t_id
    assert isinstance(data["clientDoneAt"], int)

    # Verify event logged
    events = store.list_events(board_id)
    done_events = [e for e in events if e["action"] == "client_done"]
    assert len(done_events) == 1
    assert done_events[0]["actor"] == "client"
    assert done_events[0]["taskId"] == t_id
    assert done_events[0]["payload"]["clientDoneAt"] == data["clientDoneAt"]

    # 2. Undone tick: done -> False
    resp_undone = c.post(f"/{slug}/b/{raw_token}/tasks/{t_id}/done", json={"done": False})
    assert resp_undone.status_code == 200
    data_undone = resp_undone.json()
    assert data_undone["id"] == t_id
    assert data_undone["clientDoneAt"] is None

    events2 = store.list_events(board_id)
    undone_events = [e for e in events2 if e["action"] == "client_undone"]
    assert len(undone_events) == 1
    assert undone_events[0]["actor"] == "client"
    assert undone_events[0]["taskId"] == t_id
    assert undone_events[0]["payload"]["clientDoneAt"] is None

    # 3. Wrong token or wrong slug -> identical 404
    bad_t = "tok" + "_random_nonexistent"
    r_bad_token = c.post(f"/{slug}/b/{bad_t}/tasks/{t_id}/done", json={"done": True})
    r_bad_slug = c.post(f"/wrong-slug-xyz/b/{raw_token}/tasks/{t_id}/done", json={"done": True})

    assert r_bad_token.status_code == 404
    assert r_bad_slug.status_code == 404
    assert r_bad_token.content == r_bad_slug.content
    assert r_bad_token.content == b'{"detail":"board not found"}'
    assert r_bad_token.headers["cache-control"] == "no-store"
    assert r_bad_slug.headers["cache-control"] == "no-store"

    # 4. Task of another board -> 404 identical
    r_other = c.post(f"/{slug}/b/{raw_token}/tasks/{t2['id']}/done", json={"done": True})
    assert r_other.status_code == 404
    assert r_other.content == r_bad_token.content

    # 5. Task not client-visible -> 404 identical
    r_not_vis = c.post(f"/{slug}/b/{raw_token}/tasks/{t_hidden['id']}/done", json={"done": True})
    assert r_not_vis.status_code == 404
    assert r_not_vis.content == r_bad_token.content

    # 6. Task not in waiting -> 404 identical
    r_not_waiting = c.post(f"/{slug}/b/{raw_token}/tasks/{t_doing['id']}/done", json={"done": True})
    assert r_not_waiting.status_code == 404
    assert r_not_waiting.content == r_bad_token.content

    # 7. Archived task -> 404 identical
    store.update_task(board_id, t_id, archived=True)
    r_archived = c.post(f"/{slug}/b/{raw_token}/tasks/{t_id}/done", json={"done": True})
    assert r_archived.status_code == 404
    assert r_archived.content == r_bad_token.content
    store.update_task(board_id, t_id, archived=False)

    # 8. Extra fields -> 422
    r_extra = c.post(f"/{slug}/b/{raw_token}/tasks/{t_id}/done", json={"done": True, "extra": "payload"})
    assert r_extra.status_code == 422

    # 9. Rate limit -> 429 after 30 writes
    # Already did 2 writes above, do 28 more
    for _ in range(28):
        res = c.post(f"/{slug}/b/{raw_token}/tasks/{t_id}/done", json={"done": True})
        assert res.status_code == 200

    # 31st write: rate limited
    r_limited = c.post(f"/{slug}/b/{raw_token}/tasks/{t_id}/done", json={"done": True})
    assert r_limited.status_code == 429
    assert r_limited.headers["cache-control"] == "no-store"
    assert r_limited.json()["detail"] == "too many requests"


def test_reset_on_status_change(boards_client):
    """Test client_done_at clears when leaving waiting, and moving back starts clean."""
    slug = "reset-board"
    board, raw_token = store.create_board(chat_id="-100999", slug=slug)
    board_id = board["id"]

    task = store.create_task(board_id, title_i18n={"en": "Waiting Task"}, status="waiting")
    task_id = task["id"]

    # Mark done
    store.set_task_client_done(board_id, task_id, done=True)
    t_after_done = store.get_task(task_id)
    assert t_after_done["clientDoneAt"] is not None

    # Update non-status fields (e.g. price) -> clientDoneAt is preserved
    store.update_task(board_id, task_id, price=75.0)
    assert store.get_task(task_id)["clientDoneAt"] is not None

    # Move task out of waiting to doing -> clientDoneAt is cleared
    store.update_task(board_id, task_id, status="doing")
    t_doing = store.get_task(task_id)
    assert t_doing["status"] == "doing"
    assert t_doing["clientDoneAt"] is None

    # Move task back to waiting -> starts clean (None)
    store.update_task(board_id, task_id, status="waiting")
    t_waiting_again = store.get_task(task_id)
    assert t_waiting_again["status"] == "waiting"
    assert t_waiting_again["clientDoneAt"] is None


def test_agent_and_admin_client_done_permissions(boards_client):
    """Test agent can see and clear (clientDone: false) but cannot set (clientDone: true -> 422); admin can do both."""
    login(boards_client)
    lane = make_lane(boards_client, slug="perm-lane", chat_id="-100666")
    bridge_header = {"X-Bridge-Token": lane["apiKey"]}

    b_res = boards_client.post(
        "/admin/api/boards",
        json={"chatId": "-100666", "slug": "perm-board", "title": "Perm Board"},
    )
    assert b_res.status_code == 201
    board_id = b_res.json()["id"]

    t_res = boards_client.post(
        f"/admin/api/boards/{board_id}/tasks",
        json={"titleI18n": {"en": "Perm task"}, "status": "waiting"},
    )
    task_id = t_res.json()["id"]

    # Admin sets clientDone: true
    adm_set = boards_client.patch(
        f"/admin/api/boards/{board_id}/tasks/{task_id}",
        json={"clientDone": True},
    )
    assert adm_set.status_code == 200
    assert adm_set.json()["clientDoneAt"] is not None

    # Agent reads board -> sees clientDoneAt
    agent_board = boards_client.get("/perm-lane/board", headers=bridge_header).json()
    agent_task = next(t for t in agent_board["tasks"] if t["id"] == task_id)
    assert agent_task["clientDoneAt"] is not None

    # Agent attempts to set clientDone: true -> 422
    agent_set = boards_client.patch(
        f"/perm-lane/board/tasks/{task_id}",
        headers=bridge_header,
        json={"clientDone": True},
    )
    assert agent_set.status_code == 422
    assert "cannot set" in agent_set.json()["detail"].lower()

    # Agent clears clientDone: false -> 200
    agent_clear = boards_client.patch(
        f"/perm-lane/board/tasks/{task_id}",
        headers=bridge_header,
        json={"clientDone": False},
    )
    assert agent_clear.status_code == 200
    assert agent_clear.json()["clientDoneAt"] is None

    # Admin clears clientDone: false -> 200
    # First admin sets it again
    boards_client.patch(f"/admin/api/boards/{board_id}/tasks/{task_id}", json={"clientDone": True})
    assert store.get_task(task_id)["clientDoneAt"] is not None
    # Then admin clears it
    adm_clear = boards_client.patch(
        f"/admin/api/boards/{board_id}/tasks/{task_id}",
        json={"clientDone": False},
    )
    assert adm_clear.status_code == 200
    assert adm_clear.json()["clientDoneAt"] is None


def test_data_json_whitelist_waiting_vs_other_tasks(boards_client):
    """Test data.json whitelist: id and clientDoneAt ONLY on waiting tasks; no internal leaks."""
    c = boards_client
    slug = "whitelist-board"
    board, raw_token = store.create_board(
        chat_id="-100444",
        slug=slug,
        title="Whitelist Board",
        client_lang="uk",
    )
    board_id = board["id"]

    hidden_note = "super" + "_secret_internal_value_777"
    hidden_grp = "internal" + "_group_name_444"

    t_wait = store.create_task(
        board_id,
        title_i18n={"uk": "Задача в очікуванні"},
        status="waiting",
        price=10.0,
        grp=hidden_grp,
        internal_note=hidden_note,
        client_visible=True,
    )
    store.set_task_client_done(board_id, t_wait["id"], done=True)

    t_doing = store.create_task(
        board_id,
        title_i18n={"uk": "Задача в роботі"},
        status="doing",
        price=20.0,
        grp=hidden_grp,
        internal_note=hidden_note,
        client_visible=True,
    )

    t_queue = store.create_task(
        board_id,
        title_i18n={"uk": "Задача в черзі"},
        status="queue",
        price=30.0,
        client_visible=True,
    )

    resp = c.get(f"/{slug}/b/{raw_token}/data.json?lang=uk")
    assert resp.status_code == 200
    raw_body = resp.text

    # Verify no leaks of internal values
    assert hidden_note not in raw_body
    assert hidden_grp not in raw_body
    assert "internal_note" not in raw_body
    assert "internalNote" not in raw_body
    assert "grp" not in raw_body

    # The ID of non-waiting tasks MUST NOT leak
    assert t_doing["id"] not in raw_body
    assert t_queue["id"] not in raw_body
    # The ID of waiting task MUST be present
    assert t_wait["id"] in raw_body

    data = resp.json()
    tasks_by_status = {t["status"]: t for t in data["tasks"]}

    # Waiting task has id and clientDoneAt
    waiting_task = tasks_by_status["waiting"]
    assert set(waiting_task.keys()) == {"title", "note", "status", "pay", "price", "position", "updatedAt", "id", "clientDoneAt"}
    assert waiting_task["id"] == t_wait["id"]
    assert isinstance(waiting_task["clientDoneAt"], int)

    # Doing task keeps strict non-id key set
    doing_task = tasks_by_status["doing"]
    assert set(doing_task.keys()) == {"title", "note", "status", "pay", "price", "position", "updatedAt"}
    assert "id" not in doing_task
    assert "clientDoneAt" not in doing_task

    # Queue task keeps strict non-id key set
    queue_task = tasks_by_status["queue"]
    assert set(queue_task.keys()) == {"title", "note", "status", "pay", "price", "position", "updatedAt"}
    assert "id" not in queue_task
    assert "clientDoneAt" not in queue_task


def test_panel_html_substring_entry_points():
    """Verify app.html and client.html contain the new entry points."""
    from pathlib import Path
    app_html = (Path(__file__).parent.parent / "app" / "static" / "app.html").read_text(encoding="utf-8")
    client_html = (Path(__file__).parent.parent / "app" / "boards" / "static" / "client.html").read_text(encoding="utf-8")

    # Operator panel app.html entry points
    assert "Client marked done:" in app_html
    assert "bd-btn-client-done-filter" in app_html
    assert "bd-chip-client-done" in app_html
    assert "bd-cd-clear-client-done" in app_html
    assert "bd-cd-client-done-wrap" in app_html

    # Client page client.html entry points
    assert "btn-client-done" in client_html
    assert "client-done-cancel" in client_html
    assert "client-done-status" in client_html
    assert "aria-pressed" in client_html
    assert "/done" in client_html
