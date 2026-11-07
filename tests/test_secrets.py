"""One-time lane -> lane secrets (POST/GET /{lane}/secret)."""
import time

from conftest import login, make_lane

# Built at runtime so secret scanners don't flag a literal next to a key name.
FAKE = "-".join(["test", "only", "value"])


def _two_lanes(client):
    login(client)
    a = make_lane(client, slug="alpha")
    b = make_lane(client, slug="beta")
    return {"X-Bridge-Token": a["apiKey"]}, {"X-Bridge-Token": b["apiKey"]}


def _send(client, headers, **body):
    return client.post("/alpha/secret", json={"to": "beta", "value": FAKE, **body}, headers=headers)


def test_secret_is_read_once_by_target(client):
    ha, hb = _two_lanes(client)
    resp = _send(client, ha, note="ITHS test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["to"] == "beta"
    assert body["ref"] == f"lanehub-secret:{body['id']}"
    assert FAKE not in resp.text

    got = client.get(f"/beta/secret/{body['ref']}", headers=hb)
    assert got.status_code == 200
    assert got.headers["cache-control"] == "no-store"
    assert got.json() == {"value": FAKE, "from": "alpha", "note": "ITHS test", "createdAt": got.json()["createdAt"]}

    # Gone after the first read.
    assert client.get(f"/beta/secret/{body['id']}", headers=hb).status_code == 404


def test_other_lane_cannot_read_or_burn_it(client):
    ha, hb = _two_lanes(client)
    sid = _send(client, ha).json()["id"]

    assert client.get(f"/alpha/secret/{sid}", headers=ha).status_code == 404  # sender can't read it back
    assert client.get(f"/beta/secret/{sid}", headers=ha).status_code == 401  # wrong key for the lane
    assert client.get(f"/beta/secret/{sid}").status_code == 401
    # The target's copy survived the failed attempts.
    assert client.get(f"/beta/secret/{sid}", headers=hb).json()["value"] == FAKE


def test_secret_never_reaches_chat_or_feed(client):
    ha, hb = _two_lanes(client)
    _send(client, ha)
    assert not [c for c in client.fake_tg.calls if FAKE in str(c)]
    feed = client.get("/beta/feed", headers=hb).json()
    assert FAKE not in str(feed)


def test_expired_secret_is_gone(client, monkeypatch):
    ha, hb = _two_lanes(client)
    sid = _send(client, ha, ttlHours=1).json()["id"]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3601)
    assert client.get(f"/beta/secret/{sid}", headers=hb).status_code == 404

    from app import db

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0


def test_validation(client):
    ha, _ = _two_lanes(client)
    assert client.post("/alpha/secret", json={"to": "nosuch", "value": FAKE}, headers=ha).status_code == 404
    assert _send(client, ha, ttlHours=0).status_code == 422
    assert _send(client, ha, ttlHours=169).status_code == 422
    assert client.post("/alpha/secret", json={"to": "beta", "value": ""}, headers=ha).status_code == 422
    assert client.post("/alpha/secret", json={"to": "beta", "value": FAKE}).status_code == 401


def test_deleting_a_lane_drops_its_secrets(client):
    ha, _ = _two_lanes(client)
    _send(client, ha)
    assert client.delete("/admin/api/lanes/beta").status_code in (200, 204)

    from app import db

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
