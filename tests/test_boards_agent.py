import secrets
from app.boards import store
from tests.conftest import login, make_lane


def test_agent_access_and_lifecycle(boards_client):
    c = boards_client
    login(c)
    chat_a = "-100111222"
    chat_b = "-100333444"

    lane_a = make_lane(c, slug="lane-a", chat_id=chat_a)
    key_a = lane_a["apiKey"]

    lane_b = make_lane(c, slug="lane-b", chat_id=chat_b)
    key_b = lane_b["apiKey"]

    lane_unbound = make_lane(c, slug="lane-unbound", chat_id="")
    key_unbound = lane_unbound["apiKey"]

    # Create boards in DB directly or via admin
    b_a, _ = store.create_board(chat_id=chat_a, slug="board-a", title="Board A")
    b_b, _ = store.create_board(chat_id=chat_b, slug="board-b", title="Board B")

    # 1. Unbound lane -> 503
    resp = c.get("/lane-unbound/board", headers={"X-Bridge-Token": key_unbound})
    assert resp.status_code == 503
    assert "not bound" in resp.json()["detail"]

    # 2. Wrong key -> 401
    bad_key = secrets.token_urlsafe(16)
    resp = c.get("/lane-a/board", headers={"X-Bridge-Token": bad_key})
    assert resp.status_code == 401

    # 3. Reading own board -> 200
    resp = c.get("/lane-a/board", headers={"X-Bridge-Token": key_a})
    assert resp.status_code == 200
    data = resp.json()
    assert data["board"]["slug"] == "board-a"
    assert "clientTokenHash" not in data["board"]
    assert "columns" in data
    assert "ledger" in data
    assert "tasks" in data

    # 4. Lane A requesting board B (another chat's board) -> 404
    resp = c.get("/lane-a/board?board=board-b", headers={"X-Bridge-Token": key_a})
    assert resp.status_code == 404

    # 5. Lane A creating task on board A
    resp = c.post(
        "/lane-a/board/tasks",
        headers={"X-Bridge-Token": key_a},
        json={
            "status": "queue",
            "titleI18n": {"ru": "Тест задача", "en": "Test task"},
            "pay": "deposit",
            "price": 100.0,
            "internalNote": "Internal secret",
        },
    )
    assert resp.status_code == 201
    task = resp.json()
    assert task["status"] == "queue"
    assert task["price"] == 100.0
    assert task["updatedBy"] == "lane-a"
    task_id = task["id"]

    # Missing all titles -> 422
    resp = c.post(
        "/lane-a/board/tasks",
        headers={"X-Bridge-Token": key_a},
        json={"titleI18n": {"ru": "   "}},
    )
    assert resp.status_code == 422

    # 6. Lane A updating task
    resp = c.patch(
        f"/lane-a/board/tasks/{task_id}",
        headers={"X-Bridge-Token": key_a},
        json={"status": "doing", "position": 15.0, "version": 1},
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["status"] == "doing"
    assert updated["position"] == 15.0
    assert updated["version"] == 2
    assert updated["updatedBy"] == "lane-a"

    # Version conflict -> 409
    resp = c.patch(
        f"/lane-a/board/tasks/{task_id}",
        headers={"X-Bridge-Token": key_a},
        json={"status": "done", "version": 1},
    )
    assert resp.status_code == 409
    conflict_body = resp.json()
    assert conflict_body["detail"] == "version conflict"
    assert conflict_body["task"]["version"] == 2

    # 7. Lane B trying to update task on board A -> 404
    resp = c.patch(
        f"/lane-b/board/tasks/{task_id}",
        headers={"X-Bridge-Token": key_b},
        json={"status": "done"},
    )
    assert resp.status_code == 404

    # 8. Agents cannot delete tasks (DELETE /{lane_slug}/board/tasks/{id} has no route -> 405/404)
    resp = c.delete(f"/lane-a/board/tasks/{task_id}", headers={"X-Bridge-Token": key_a})
    assert resp.status_code in (404, 405)

    # Agents cannot touch ledger or settings
    resp = c.post("/lane-a/board/ledger", headers={"X-Bridge-Token": key_a}, json={})
    assert resp.status_code in (404, 405)
    resp = c.patch("/lane-a/board", headers={"X-Bridge-Token": key_a}, json={})
    assert resp.status_code in (404, 405)

    # 9. Events endpoint
    resp = c.get("/lane-a/board/events", headers={"X-Bridge-Token": key_a})
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 2
    actions = [e["action"] for e in events]
    assert "task_created" in actions
    assert "task_updated" in actions
