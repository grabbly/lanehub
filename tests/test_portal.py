"""Member portal: invitations, self-service lane creation, project chat."""
from conftest import login, member_login


def invite(client, email="denis@example.org", name="Denis"):
    resp = client.post("/admin/api/members", json={"email": email, "name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def portal_login(client, email, password):
    return member_login(client, email, password)


def test_invite_returns_credentials_and_text(client):
    login(client)
    p = invite(client)
    assert p["email"] == "denis@example.org"
    assert len(p["password"]) >= 8
    assert p["loginUrl"] == "/"  # unified sign-in (no public base url in tests)
    assert p["password"] in p["inviteText"] and p["loginUrl"] in p["inviteText"]
    assert p["emailSent"] is False  # no SMTP in tests
    dup = client.post("/admin/api/members", json={"email": "denis@example.org"})
    assert dup.status_code == 409
    bad = client.post("/admin/api/members", json={"email": "not-an-email"})
    assert bad.status_code == 422


def test_portal_requires_login(client):
    assert client.get("/portal/api/me").status_code == 401
    assert client.post("/portal/api/lane", json={"botToken": "t:1"}).status_code == 401


def test_full_member_flow(client):
    login(client)
    client.patch("/admin/api/settings", json={"projectChatId": "-100777"})
    p = invite(client)

    assert portal_login(client, p["email"], "wrong").status_code == 401
    assert portal_login(client, p["email"], p["password"]).status_code == 200

    me = client.get("/portal/api/me").json()
    assert me["email"] == p["email"] and me["lane"] is None
    assert me["projectChatId"] == "-100777"

    # member connects their own bot; lane inherits the project chat
    lane = client.post("/portal/api/lane", json={"botToken": "denis-token:1"})
    assert lane.status_code == 201, lane.text
    lane = lane.json()
    assert lane["slug"] == "test"  # from fake bot username test_bot
    assert lane["defaultChatId"] == "-100777"
    assert lane["apiKey"]

    # the lane key actually works on the bridge API
    sent = client.post(f"/{lane['slug']}/send", json={"text": "hi"}, headers={"X-Bridge-Token": lane["apiKey"]})
    assert sent.status_code == 200 and sent.json()["chatId"] == -100777

    # only one lane per member
    again = client.post("/portal/api/lane", json={"botToken": "denis-token:2"})
    assert again.status_code == 409

    # admin sees the linkage (one cookie per session: re-auth as admin first)
    login(client)
    members = client.get("/admin/api/members").json()["members"]
    assert members[0]["laneSlug"] == "test"


def test_member_rotate_and_password_change(client):
    login(client)
    p = invite(client, email="ilya@example.org")
    portal_login(client, p["email"], p["password"])
    client.post("/portal/api/lane", json={"botToken": "t:9"})

    old_key = client.get("/portal/api/me").json()["lane"]["apiKey"]
    new_key = client.post("/portal/api/lane/rotate-key").json()["apiKey"]
    assert new_key != old_key

    bad = client.post("/portal/api/password", json={"oldPassword": "nope", "newPassword": "longenough1"})
    assert bad.status_code == 401
    ok = client.post("/portal/api/password", json={"oldPassword": p["password"], "newPassword": "longenough1"})
    assert ok.status_code == 200
    client.post("/api/logout")
    assert portal_login(client, p["email"], p["password"]).status_code == 401
    assert portal_login(client, p["email"], "longenough1").status_code == 200


def test_reset_password_and_delete_member(client):
    login(client)
    p = invite(client, email="kostya@example.org")
    p2 = client.post(f"/admin/api/members/{p['email']}/reset-password").json()
    assert p2["password"] != p["password"]
    assert portal_login(client, p["email"], p["password"]).status_code == 401
    assert portal_login(client, p["email"], p2["password"]).status_code == 200

    login(client)  # one cookie per session: re-auth as admin to delete
    assert client.delete(f"/admin/api/members/{p['email']}").status_code == 200
    # the account is gone — the member can no longer authenticate at all
    assert portal_login(client, p["email"], p2["password"]).status_code == 401


def test_send_falls_back_to_project_chat(client):
    login(client)
    # lane created BEFORE the project chat is set, with no default chat
    from conftest import make_lane

    lane = make_lane(client, slug="late", chat_id="")
    client.patch("/admin/api/settings", json={"projectChatId": "-100888"})
    resp = client.post("/late/send", json={"text": "hi"}, headers={"X-Bridge-Token": lane["apiKey"]})
    assert resp.status_code == 200 and resp.json()["chatId"] == -100888
