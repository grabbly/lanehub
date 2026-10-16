"""«Куди пішов депозит»: depositHistory in the client data.json.

The breakdown must add up to derive_ledger() exactly (archived, refunds, hidden
cards included) and expose nothing but kind / title / status / amount / date.
"""
from pathlib import Path

import pytest

from app.boards import store

ALLOWED_KEYS = {
    "deposit": {"kind", "amount", "date"},
    "refund": {"kind", "amount", "date"},
    "task": {"kind", "title", "status", "amount", "date"},
    "other": {"kind", "amount"},
}


def _totals(items):
    deposit = sum(i["amount"] for i in items if i["kind"] == "deposit") - sum(
        i["amount"] for i in items if i["kind"] == "refund"
    )
    allocated = sum(i["amount"] for i in items if i["kind"] in ("task", "other"))
    return deposit, allocated


def _seed(slug="dep-board"):
    board, link = store.create_board(
        chat_id="-100777", slug=slug, title="Deposit Board", team_lang="ru", client_lang="uk", currency="zł"
    )
    bid = board["id"]
    store.create_ledger_entry(bid, kind="payment", amount=550.0, at="2026-10-01", note="stage one")
    store.create_ledger_entry(bid, kind="deposit", amount=400.0, at="2026-10-02", note="LEDGER-NOTE-PRIVATE")
    store.create_ledger_entry(bid, kind="deposit", amount=100.0, at="2026-10-10")
    store.create_ledger_entry(bid, kind="refund", amount=50.0, at="2026-10-12")
    t_done = store.create_task(
        bid, title_i18n={"ru": "Лендинг RU", "uk": "Лендинг UK", "en": "Landing EN"},
        status="done", pay="deposit", price=200.0, hours=5.25,
        grp="GRP-PRIVATE", source="#SRC-PRIVATE", internal_note="INTERNAL-PRIVATE",
    )
    store.create_task(bid, title_i18n={"uk": "Форма UK"}, status="doing", pay="deposit", price=80.0)
    t_arch = store.create_task(bid, title_i18n={"uk": "Архівна UK"}, status="done", pay="deposit", price=40.0)
    store.update_task(bid, t_arch["id"], archived=True)
    store.create_task(
        bid, title_i18n={"uk": "HIDDEN-TITLE-PRIVATE"}, status="queue", pay="deposit", price=40.0, client_visible=False
    )
    # Not from the deposit: must not appear at all.
    store.create_task(bid, title_i18n={"uk": "Окремо UK"}, status="doing", pay="paid", price=999.0)
    return bid, link, t_done, t_arch


def test_history_sums_match_ledger(boards_client):
    bid, _, _, _ = _seed()
    ledger = store.derive_ledger(bid)
    items = store.deposit_history(bid, "uk", "uk")

    deposit, allocated = _totals(items)
    assert deposit == pytest.approx(ledger["deposit"]) == pytest.approx(450.0)
    assert allocated == pytest.approx(ledger["allocated"]) == pytest.approx(360.0)
    assert deposit - allocated == pytest.approx(ledger["remaining"]) == pytest.approx(90.0)
    assert ledger["paid"] == pytest.approx(550.0)

    # Same numbers from the single-read helper the route uses.
    ledger2, items2 = store.ledger_with_deposit_history(bid, "uk", "uk")
    assert ledger2 == ledger
    assert items2 == items

    kinds = [i["kind"] for i in items]
    assert "payment" not in kinds
    assert kinds.count("deposit") == 2 and kinds.count("refund") == 1
    titles = {i.get("title") for i in items if i["kind"] == "task"}
    assert titles == {"Лендинг UK", "Форма UK", "Архівна UK"}  # archived counted, pay='paid' not
    # Hidden card: aggregated into one untitled line, listed last.
    assert items[-1] == {"kind": "other", "amount": 40.0}
    assert kinds.count("other") == 1


def test_history_order_titles_and_dates(boards_client):
    bid, _, t_done, t_arch = _seed()
    items = store.deposit_history(bid, "en", "uk")
    dated = [i for i in items if "date" in i]
    assert [i["date"] for i in dated] == sorted((i["date"] for i in dated), reverse=True)
    by_title = {i["title"]: i for i in items if i["kind"] == "task"}
    # Viewer language first, client language as fallback (same rule as the cards).
    assert "Landing EN" in by_title and "Форма UK" in by_title
    assert by_title["Landing EN"]["status"] == "done"
    assert by_title["Форма UK"]["status"] == "doing"
    assert {i["date"] for i in items if i["kind"] == "deposit"} == {"2026-10-02", "2026-10-10"}
    for i in by_title.values():
        assert len(i["date"]) == 10 and i["date"][4] == "-"


def test_history_empty_and_hidden_only(boards_client):
    board, _ = store.create_board(chat_id="-100778", slug="empty-dep", title="E")
    bid = board["id"]
    assert store.deposit_history(bid, "uk", "uk") == []
    assert store.derive_ledger(bid) == {"paid": 0.0, "deposit": 0.0, "allocated": 0.0, "remaining": 0.0}

    store.create_task(bid, title_i18n={"uk": "Прихована"}, pay="deposit", price=25.0, client_visible=False)
    items = store.deposit_history(bid, "uk", "uk")
    assert items == [{"kind": "other", "amount": 25.0}]
    assert store.derive_ledger(bid)["remaining"] == pytest.approx(-25.0)


def test_data_json_deposit_history_whitelist(boards_client):
    bid, link, t_done, t_arch = _seed()
    resp = boards_client.get(f"/dep-board/b/{link}/data.json?lang=uk")
    assert resp.status_code == 200
    raw = resp.text
    for private in (
        "LEDGER-NOTE-PRIVATE", "stage one", "INTERNAL-PRIVATE", "GRP-PRIVATE", "#SRC-PRIVATE",
        "HIDDEN-TITLE-PRIVATE", "5.25", t_done["id"], t_arch["id"], "internalNote", "hours",
    ):
        assert private not in raw, private

    data = resp.json()
    items = data["depositHistory"]
    assert items
    for i in items:
        assert set(i.keys()) == ALLOWED_KEYS[i["kind"]]
    deposit, allocated = _totals(items)
    assert deposit == pytest.approx(data["ledger"]["deposit"])
    assert allocated == pytest.approx(data["ledger"]["allocated"])
    assert data["ledger"]["remaining"] == pytest.approx(90.0)


def test_client_html_has_deposit_dialog():
    html = (Path(store.__file__).parent / "static" / "client.html").read_text(encoding="utf-8")
    for needle in ('id="dep-dialog"', 'id="cell-used"', 'id="cell-rest"', 'aria-haspopup="dialog"',
                   "depositHistory", "showModal", "deposit_history_title", 'id="dep-close"'):
        assert needle in html, needle
    assert "innerHTML" not in html  # titles go in via textContent only
