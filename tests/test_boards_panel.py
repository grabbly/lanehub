"""Smoke tests for the Boards module UI in the operator panel (app/static/app.html)."""
from __future__ import annotations


def test_boards_panel_markup_and_entry_points(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Tab markup
    assert 'data-tab="boards"' in html
    assert 'id="tab-btn-boards"' in html
    assert 'id="tab-boards"' in html

    # Main views and components
    assert 'class="bd-main"' in html
    assert 'id="bd-board-view"' in html
    assert 'id="bd-empty-state"' in html
    assert 'id="bd-board-select"' in html
    assert 'id="bd-board-cols"' in html
    assert 'class="bd-ledger"' in html
    assert 'class="bd-toolbar"' in html

    # Board Dialogs
    assert 'id="bd-new-board-dlg"' in html
    assert 'id="bd-link-once-dlg"' in html
    assert 'id="bd-client-link-dlg"' in html
    assert 'id="bd-settings-dlg"' in html
    assert 'id="bd-ledger-dlg"' in html
    assert 'id="bd-card-dlg"' in html

    # JS entry points and functions
    assert "function renderBoardsTab" in html
    assert "function boardApi" in html
    assert "function openBoardForChat" in html
    assert "function openTaskDialog" in html
    assert "function moveTaskToPosition" in html
    assert "function startBoardFreshness" in html
    assert "data-open-board" in html


def test_boards_panel_admin_flow(boards_client):
    from tests.conftest import login, make_lane

    login(boards_client)
    make_lane(boards_client, slug="devlane", chat_id="-100999")

    # 1. Create a board
    create_res = boards_client.post(
        "/admin/api/boards",
        json={
            "chatId": "-100999",
            "slug": "project-alpha",
            "title": "Alpha Project",
            "teamLang": "en",
            "clientLang": "uk",
            "rate": 50.0,
            "currency": "zł",
        },
    )
    assert create_res.status_code == 201
    board_data = create_res.json()
    board_id = board_data["id"]
    client_path = board_data["clientPath"]
    assert "/project-alpha/b/" in client_path

    # 2. Create task
    task_res = boards_client.post(
        f"/admin/api/boards/{board_id}/tasks",
        json={
            "status": "backlog",
            "grp": "Core",
            "source": "#101",
            "pay": "deposit",
            "price": 150.0,
            "hours": 3.0,
            "clientVisible": True,
            "titleI18n": {"en": "Feature A", "uk": "Фіча А"},
            "clientNoteI18n": {"uk": "В роботі"},
            "internalNote": "Needs testing",
        },
    )
    assert task_res.status_code == 201
    task = task_res.json()
    task_id = task["id"]
    assert task["version"] == 1

    # 3. Patch task position and status
    patch_res = boards_client.patch(
        f"/admin/api/boards/{board_id}/tasks/{task_id}",
        json={
            "status": "doing",
            "position": 20.0,
            "version": 1,
        },
    )
    assert patch_res.status_code == 200
    patched_task = patch_res.json()
    assert patched_task["status"] == "doing"
    assert patched_task["version"] == 2

    # 4. Patch with stale version -> 409 conflict
    conflict_res = boards_client.patch(
        f"/admin/api/boards/{board_id}/tasks/{task_id}",
        json={
            "status": "done",
            "version": 1,
        },
    )
    assert conflict_res.status_code == 409
    conflict_body = conflict_res.json()
    assert "version conflict" in conflict_body["detail"]

    # 5. Add ledger deposit
    ledger_res = boards_client.post(
        f"/admin/api/boards/{board_id}/ledger",
        json={
            "kind": "deposit",
            "amount": 500.0,
            "at": "2026-10-14",
            "note": "Initial deposit",
        },
    )
    assert ledger_res.status_code == 201

    # 6. Check board detail derivation
    board_detail = boards_client.get(f"/admin/api/boards/{board_id}").json()
    assert board_detail["ledger"]["deposit"] == 500.0
    assert board_detail["ledger"]["allocated"] == 150.0
    assert board_detail["ledger"]["remaining"] == 350.0

