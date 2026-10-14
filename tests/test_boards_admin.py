import secrets
from tests.conftest import login, make_lane


def test_admin_auth_and_board_lifecycle(boards_client):
    c = boards_client

    # 1. Unauthenticated requests -> 401
    resp = c.get("/admin/api/boards")
    assert resp.status_code == 401

    resp = c.post("/admin/api/boards", json={"chatId": "-100", "slug": "test"})
    assert resp.status_code == 401

    # Login as admin
    login(c)

    # 2. Slug clash with existing lane -> 409
    make_lane(c, slug="clashing-lane")
    resp = c.post(
        "/admin/api/boards",
        json={"chatId": "-100", "slug": "clashing-lane", "title": "Clash"},
    )
    assert resp.status_code == 409

    # 3. Create board -> 201, returns clientToken once and clientPath
    resp = c.post(
        "/admin/api/boards",
        json={
            "chatId": "-100555",
            "slug": "client-board",
            "title": "Client Board",
            "teamLang": "ru",
            "clientLang": "uk",
            "currency": "zł",
            "rate": 40.0,
        },
    )
    assert resp.status_code == 201
    created = resp.json()
    board_id = created["id"]
    client_token = created["clientToken"]
    client_path = created["clientPath"]
    assert client_token is not None
    assert f"/{created['slug']}/b/{client_token}/" == client_path

    # Verify GET /{id} and GET "" never return clientToken or clientTokenHash
    resp = c.get(f"/admin/api/boards/{board_id}")
    assert resp.status_code == 200
    b_detail = resp.json()
    assert "clientToken" not in b_detail
    assert "clientTokenHash" not in b_detail

    resp = c.get("/admin/api/boards")
    assert resp.status_code == 200
    b_list = resp.json()
    assert len(b_list) >= 1
    for b in b_list:
        assert "clientToken" not in b
        assert "clientTokenHash" not in b

    # Verify client path works
    resp = c.get(client_path)
    assert resp.status_code == 200

    # 4. Rotate token kills old link
    resp = c.post(f"/admin/api/boards/{board_id}/rotate-client-token")
    assert resp.status_code == 200
    rotated = resp.json()
    new_token = rotated["clientToken"]
    new_path = rotated["clientPath"]
    assert new_token != client_token

    # Old token gives 404
    resp = c.get(client_path)
    assert resp.status_code == 404

    # New token gives 200
    resp = c.get(new_path)
    assert resp.status_code == 200

    # 5. Tasks CRUD via admin
    resp = c.post(
        f"/admin/api/boards/{board_id}/tasks",
        json={
            "titleI18n": {"en": "Admin Task"},
            "status": "backlog",
            "pay": "estimate",
            "price": 200.0,
        },
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    resp = c.get(f"/admin/api/boards/{board_id}/tasks")
    assert resp.status_code == 200
    assert any(t["id"] == task_id for t in resp.json())

    resp = c.patch(
        f"/admin/api/boards/{board_id}/tasks/{task_id}",
        json={"price": 250.0},
    )
    assert resp.status_code == 200
    assert resp.json()["price"] == 250.0

    resp = c.delete(f"/admin/api/boards/{board_id}/tasks/{task_id}")
    assert resp.status_code == 200

    # 6. Ledger CRUD via admin
    resp = c.post(
        f"/admin/api/boards/{board_id}/ledger",
        json={"kind": "payment", "amount": 550.0, "at": "2026-10-13", "note": "Payment 1"},
    )
    assert resp.status_code == 201
    entry_id = resp.json()["id"]

    resp = c.get(f"/admin/api/boards/{board_id}/ledger")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = c.patch(
        f"/admin/api/boards/{board_id}/ledger/{entry_id}",
        json={"amount": 600.0},
    )
    assert resp.status_code == 200
    assert resp.json()["amount"] == 600.0

    resp = c.delete(f"/admin/api/boards/{board_id}/ledger/{entry_id}")
    assert resp.status_code == 200

    # 7. Delete board
    resp = c.delete(f"/admin/api/boards/{board_id}")
    assert resp.status_code == 200
    resp = c.get(f"/admin/api/boards/{board_id}")
    assert resp.status_code == 404
