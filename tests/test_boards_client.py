import secrets
from app.boards import store


def test_client_endpoints_and_privacy(boards_client):
    c = boards_client
    slug = "public-board"
    board, client_token = store.create_board(
        chat_id="-100999",
        slug=slug,
        title="Public Board Title",
        team_lang="ru",
        client_lang="uk",
        currency="zł",
        rate=40.0,
    )
    bid = board["id"]

    hidden_note_val = "HIDDEN-INTERNAL-NOTE-VALUE"
    hidden_grp_val = "HIDDEN-GROUP-NAME"
    hidden_hours_val = 3.75

    # Visible task
    t_vis = store.create_task(
        bid,
        title_i18n={"ru": "Публичная задача RU", "uk": "Публічна задача UK"},
        client_note_i18n={"uk": "Замітка для клієнта"},
        status="doing",
        pay="deposit",
        price=120.0,
        hours=hidden_hours_val,
        grp=hidden_grp_val,
        internal_note=hidden_note_val,
        client_visible=True,
    )

    # Hidden task
    hidden_task_title = "HIDDEN-TASK-TITLE"
    t_hidden = store.create_task(
        bid,
        title_i18n={"ru": hidden_task_title},
        status="backlog",
        client_visible=False,
    )

    # Add ledger entry
    store.create_ledger_entry(bid, kind="deposit", amount=500.0, at="2026-10-14")

    # 1. Trailing slash redirect
    resp = c.get(f"/{slug}/b/{client_token}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == f"./{client_token}/"
    assert resp.headers["cache-control"] == "no-store"

    # 2. HTML page endpoint
    resp = c.get(f"/{slug}/b/{client_token}/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert resp.headers["referrer-policy"] == "no-referrer"

    # 3. data.json endpoint
    resp = c.get(f"/{slug}/b/{client_token}/data.json?lang=uk")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert resp.headers["referrer-policy"] == "no-referrer"

    raw_body = resp.text
    # Verify NO internal fields leaked anywhere in the raw response body
    assert hidden_note_val not in raw_body
    assert hidden_grp_val not in raw_body
    assert "3.75" not in raw_body
    assert hidden_task_title not in raw_body
    assert "internal_note" not in raw_body
    assert "internalNote" not in raw_body
    assert "hours" not in raw_body
    assert "grp" not in raw_body
    assert "updatedBy" not in raw_body
    assert "updated_by" not in raw_body
    assert "chatId" not in raw_body
    assert "chat_id" not in raw_body
    assert "version" not in raw_body

    data = resp.json()
    # Exact top-level key set check
    expected_top_keys = {
        "title",
        "currency",
        "clientLang",
        "langs",
        "updatedAt",
        "status",
        "statusUpdatedAt",
        "ledger",
        "ui",
        "tasks",
        "archive",
    }
    assert set(data.keys()) == expected_top_keys

    # Exact task object key set check
    assert len(data["tasks"]) == 1
    task_obj = data["tasks"][0]
    expected_task_keys = {"title", "note", "status", "pay", "price", "position", "updatedAt"}
    assert set(task_obj.keys()) == expected_task_keys

    # Check values for viewer lang UK
    assert task_obj["title"] == "Публічна задача UK"
    assert task_obj["note"] == "Замітка для клієнта"
    assert task_obj["status"] == "doing"
    assert task_obj["pay"] == "deposit"
    assert task_obj["price"] == 120.0

    # 4. Lang fallback: EN is missing, the client's language (UK) wins over the team's (RU)
    resp_en = c.get(f"/{slug}/b/{client_token}/data.json?lang=en")
    assert resp_en.status_code == 200
    data_en = resp_en.json()
    assert data_en["tasks"][0]["title"] == "Публічна задача UK"

    # Unknown lang fallback to clientLang (uk)
    resp_unk = c.get(f"/{slug}/b/{client_token}/data.json?lang=xx")
    assert resp_unk.status_code == 200
    assert resp_unk.json()["tasks"][0]["title"] == "Публічна задача UK"

    # 5. Wrong token and wrong slug give byte-identical 404 bodies
    wrong_token = secrets.token_urlsafe(16)
    resp_wrong_token = c.get(f"/{slug}/b/{wrong_token}/")
    resp_wrong_slug = c.get(f"/nonexistent-board-xyz/b/{client_token}/")

    assert resp_wrong_token.status_code == 404
    assert resp_wrong_slug.status_code == 404
    assert resp_wrong_token.content == resp_wrong_slug.content
    assert resp_wrong_token.headers["cache-control"] == "no-store"
    assert resp_wrong_slug.headers["cache-control"] == "no-store"
    assert resp_wrong_token.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert resp_wrong_slug.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert resp_wrong_token.headers["referrer-policy"] == "no-referrer"
    assert resp_wrong_slug.headers["referrer-policy"] == "no-referrer"

    # Also for data.json
    resp_data_wrong_token = c.get(f"/{slug}/b/{wrong_token}/data.json")
    resp_data_wrong_slug = c.get(f"/nonexistent-board-xyz/b/{client_token}/data.json")
    assert resp_data_wrong_token.status_code == 404
    assert resp_data_wrong_slug.status_code == 404
    assert resp_data_wrong_token.content == resp_data_wrong_slug.content
    assert resp_data_wrong_token.content == resp_wrong_token.content


def test_redirect_refuses_odd_tokens(boards_client):
    resp = boards_client.get("/anything/b/evil.com:", follow_redirects=False)
    assert resp.status_code == 404
