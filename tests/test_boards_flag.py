import secrets
from tests.conftest import login, make_lane


def test_boards_disabled_by_default(client):
    # Flag is off in client fixture
    login(client)
    lane = make_lane(client, slug="testlane")
    key = lane["apiKey"]

    resp = client.get("/testlane/board", headers={"X-Bridge-Token": key})
    assert resp.status_code == 404

    # Client route should 404
    dummy_tok = secrets.token_urlsafe(16)
    resp = client.get(f"/myboard/b/{dummy_tok}/")
    assert resp.status_code == 404

    # Admin boards route should 404
    login(client)
    resp = client.get("/admin/api/boards")
    assert resp.status_code == 404
