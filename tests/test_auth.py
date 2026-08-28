"""Unified sign-in: one form, one cookie, role resolved server-side."""
from conftest import ADMIN_PASSWORD, login, make_lane, member_login


def _invite(client, email="mia@example.org"):
    resp = client.post("/admin/api/members", json={"email": email, "name": "Mia"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_blank_email_is_admin(client):
    assert client.post("/api/login", json={"email": "", "password": ADMIN_PASSWORD}).status_code == 200
    st = client.get("/api/session").json()
    assert st["authenticated"] is True and st["role"] == "admin"
    assert client.get("/admin/api/lanes").status_code == 200


def test_email_password_is_member(client):
    login(client)
    p = _invite(client)
    assert member_login(client, p["email"], p["password"]).status_code == 200
    st = client.get("/api/session").json()
    assert st["role"] == "member"
    assert client.get("/portal/api/me").json()["email"] == p["email"]


def test_wrong_credentials(client):
    assert client.post("/api/login", json={"email": "", "password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"email": "ghost@example.org", "password": "x"}).status_code == 401


def test_roles_do_not_cross(client):
    # admin cookie cannot use member endpoints
    login(client)
    assert client.get("/portal/api/me").status_code == 401
    # member cookie cannot use admin endpoints
    p = _invite(client, email="leo@example.org")
    member_login(client, p["email"], p["password"])
    assert client.get("/admin/api/lanes").status_code == 401


def test_logout_clears_session(client):
    login(client)
    assert client.get("/api/session").json()["authenticated"] is True
    client.post("/api/logout")
    assert client.get("/api/session").json()["authenticated"] is False


def test_old_urls_redirect_to_unified(client):
    for old in ("/admin", "/portal"):
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/"


def test_root_serves_unified_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Sign in" in r.text and "/api/login" in r.text
