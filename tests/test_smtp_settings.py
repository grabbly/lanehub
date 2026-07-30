"""SMTP configured from the admin panel: storage, masking, precedence, sending."""
import pytest
from conftest import login


class FakeSMTP:
    sent: list = []
    hosts: list = []
    logins: list = []

    def __init__(self, host, port, timeout=None):
        FakeSMTP.hosts.append((host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        FakeSMTP.logins.append((user, password))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.hosts, FakeSMTP.logins = [], [], []
    monkeypatch.setattr("app.mailer.smtplib.SMTP", FakeSMTP)
    return FakeSMTP


SMTP_BODY = {
    "smtpHost": "mail.example.com",
    "smtpPort": 587,
    "smtpUser": "no-reply@example.com",
    "smtpPassword": "s3cret",
    "smtpFrom": "no-reply@example.com",
    "smtpTls": True,
}


def test_settings_roundtrip_masks_password(client):
    login(client)
    st = client.patch("/admin/api/settings", json=SMTP_BODY).json()
    assert st["smtpConfigured"] is True
    assert st["smtp"]["source"] == "panel"
    assert st["smtp"]["host"] == "mail.example.com"
    assert st["smtp"]["passwordSet"] is True
    assert "s3cret" not in str(st)  # password never echoed back

    # PATCH without password keeps the stored one
    st2 = client.patch("/admin/api/settings", json={"smtpUser": "other@example.com"}).json()
    assert st2["smtp"]["passwordSet"] is True

    # clearing the host drops panel config entirely
    st3 = client.patch("/admin/api/settings", json={"smtpHost": ""}).json()
    assert st3["smtpConfigured"] is False
    assert st3["smtp"]["source"] == "none"


def test_panel_config_beats_env(client, monkeypatch):
    from app import mailer
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "smtp_host", "env.example.com")
    assert mailer.smtp_config().source == "env"
    login(client)
    client.patch("/admin/api/settings", json=SMTP_BODY)
    c = mailer.smtp_config()
    assert c.source == "panel" and c.host == "mail.example.com"


def test_test_email_endpoint(client, fake_smtp):
    login(client)
    # not configured yet → 400
    assert client.post("/admin/api/settings/test-email", json={"to": "x@example.org"}).status_code == 400
    client.patch("/admin/api/settings", json=SMTP_BODY)
    bad = client.post("/admin/api/settings/test-email", json={"to": "not-an-email"})
    assert bad.status_code == 422
    ok = client.post("/admin/api/settings/test-email", json={"to": "x@example.org"})
    assert ok.status_code == 200, ok.text
    assert fake_smtp.hosts == [("mail.example.com", 587)]
    assert fake_smtp.logins == [("no-reply@example.com", "s3cret")]
    assert fake_smtp.sent[0]["To"] == "x@example.org"


def test_invite_emails_via_panel_smtp(client, fake_smtp):
    login(client)
    client.patch("/admin/api/settings", json=SMTP_BODY)
    p = client.post("/admin/api/members", json={"email": "denis@example.org"}).json()
    assert p["emailSent"] is True
    assert fake_smtp.sent[0]["To"] == "denis@example.org"
    assert p["password"] in fake_smtp.sent[0].get_content()
