"""Single-operator sign-in: one form, one cookie, one role."""
from conftest import ADMIN_PASSWORD, login


def test_password_is_operator(client):
    assert client.post("/api/login", json={"password": ADMIN_PASSWORD}).status_code == 200
    st = client.get("/api/session").json()
    assert st["authenticated"] is True
    assert client.get("/admin/api/lanes").status_code == 200


def test_email_field_is_ignored(client):
    # a stray email in the body is accepted but irrelevant — password is what counts
    assert client.post("/api/login", json={"email": "whoever@example.org", "password": ADMIN_PASSWORD}).status_code == 200
    assert client.get("/api/session").json()["authenticated"] is True


def test_wrong_password(client):
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.get("/api/session").json()["authenticated"] is False


def test_unauthenticated_cannot_use_admin(client):
    assert client.get("/admin/api/lanes").status_code == 401


def test_logout_clears_session(client):
    login(client)
    assert client.get("/api/session").json()["authenticated"] is True
    client.post("/api/logout")
    assert client.get("/api/session").json()["authenticated"] is False


def test_old_admin_url_redirects_to_unified(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/"


def test_portal_is_gone(client):
    # the self-service portal was removed — its URL is no longer a route
    assert client.get("/portal", follow_redirects=False).status_code == 404


def test_root_serves_unified_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Sign in" in r.text and "/api/login" in r.text
