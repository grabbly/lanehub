"""Public read-only client routes for boards (token auth in URL)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import store

router = APIRouter()

STATIC_DIR = Path(__file__).parent / "static"
CLIENT_HTML_PATH = STATIC_DIR / "client.html"
CLIENT_HTML = CLIENT_HTML_PATH.read_text(encoding="utf-8")

I18N_DIR = Path(__file__).parent / "i18n"
I18N_DICTS: dict[str, dict] = {}
for lang in store.LANGS:
    fpath = I18N_DIR / f"{lang}.json"
    if fpath.exists():
        with open(fpath, "r", encoding="utf-8") as f:
            I18N_DICTS[lang] = json.load(f)

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Referrer-Policy": "no-referrer",
}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
NOT_FOUND_BODY = b'{"detail":"board not found"}'


def _not_found_response() -> Response:
    return Response(
        content=NOT_FOUND_BODY,
        status_code=404,
        media_type="application/json",
        headers=SECURITY_HEADERS,
    )


def _verify_access(board_slug: str, token: str) -> dict | None:
    board = store.get_board_by_slug(board_slug)
    if not board:
        store.verify_client_token(token, None)
        return None
    stored_hash = store.get_board_token_hash(board["id"])
    if not store.verify_client_token(token, stored_hash):
        return None
    return board


@router.get("/{board_slug}/b/{token}")
def redirect_to_trailing_slash(board_slug: str, token: str) -> Response:
    if not TOKEN_RE.match(token):
        return _not_found_response()  # never echo arbitrary text into Location
    # Relative target: behind a proxy that maps another prefix onto this path
    # (e.g. clients.example.com/yl/<token> → /yl/b/<token>) the visitor stays on
    # the address they were given.
    return RedirectResponse(
        url=f"./{token}/",
        status_code=307,
        headers=SECURITY_HEADERS,
    )


@router.get("/{board_slug}/b/{token}/")
def get_board_page(board_slug: str, token: str) -> Response:
    board = _verify_access(board_slug, token)
    if not board:
        return _not_found_response()
    return HTMLResponse(content=CLIENT_HTML, headers=SECURITY_HEADERS)


@router.get("/{board_slug}/b/{token}/data.json")
def get_board_data(board_slug: str, token: str, lang: str | None = None) -> Response:
    board = _verify_access(board_slug, token)
    if not board:
        return _not_found_response()

    board_id = board["id"]
    active_lang = lang.strip().lower() if lang else ""
    if active_lang not in store.LANGS:
        active_lang = board["clientLang"]

    ui = I18N_DICTS.get(active_lang, I18N_DICTS.get("en", {}))

    tasks = []
    for t in store.list_tasks(board_id, client_visible_only=True):
        tasks.append({
            # Untranslated text falls back to the client's language, not the
            # team's: a Ukrainian client reading EN should see Ukrainian.
            "title": store.resolve_text(t["titleI18n"], active_lang, board["clientLang"]),
            "note": store.resolve_text(t["clientNoteI18n"], active_lang, board["clientLang"]),
            "status": t["status"],
            "pay": t["pay"],
            "price": t["price"],
            "position": t["position"],
        })

    payload = {
        "title": board["title"],
        "currency": board["currency"],
        "clientLang": board["clientLang"],
        "langs": list(store.LANGS),
        "updatedAt": store.get_board_client_updated_at(board_id),
        "ledger": store.derive_ledger(board_id),
        "ui": ui,
        "tasks": tasks,
    }
    return JSONResponse(content=payload, headers=SECURITY_HEADERS)
