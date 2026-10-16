"""Public read-only client routes for boards (token auth in URL)."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, model_validator

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
    return HTMLResponse(content=CLIENT_HTML_PATH.read_text(encoding="utf-8"), headers=SECURITY_HEADERS)


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
    for t in store.list_tasks(board_id, client_visible_only=True, archived=False):
        task_data = {
            # Untranslated text falls back to the client's language, not the
            # team's: a Ukrainian client reading EN should see Ukrainian.
            "title": store.resolve_text(t["titleI18n"], active_lang, board["clientLang"]),
            "note": store.resolve_text(t["clientNoteI18n"], active_lang, board["clientLang"]),
            "status": t["status"],
            "pay": t["pay"],
            "price": t["price"],
            "position": t["position"],
            "updatedAt": t["updatedAt"],
        }
        if t["status"] == "waiting":
            task_data["id"] = t["id"]
            task_data["clientDoneAt"] = t["clientDoneAt"]
            task_data["clientDoneComment"] = t["clientDoneComment"]
        tasks.append(task_data)

    archived_tasks = store.list_tasks(board_id, client_visible_only=True, archived=True)
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in archived_tasks:
        ts = t["archivedAt"] or t["updatedAt"]
        m_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
        groups[m_str].append(t)

    sorted_months = sorted(groups.keys(), reverse=True)
    archive = []
    for m in sorted_months:
        m_tasks = sorted(groups[m], key=lambda x: (x["archivedAt"] or 0), reverse=True)
        count = len(m_tasks)
        month_sum = sum(t["price"] for t in m_tasks if t["pay"] in ("deposit", "paid"))
        items = []
        for t in m_tasks:
            items.append({
                "title": store.resolve_text(t["titleI18n"], active_lang, board["clientLang"]),
                "price": t["price"],
                "pay": t["pay"],
            })
        archive.append({
            "month": m,
            "count": count,
            "sum": round(month_sum, 2),
            "items": items,
        })

    status_text = store.resolve_text(board["statusI18n"], active_lang, board["clientLang"])
    ledger, deposit_history = store.ledger_with_deposit_history(board_id, active_lang, board["clientLang"])

    payload = {
        "title": board["title"],
        "currency": board["currency"],
        "clientLang": board["clientLang"],
        "langs": list(store.LANGS),
        "updatedAt": store.get_board_client_updated_at(board_id),
        "status": status_text,
        "statusUpdatedAt": board["statusUpdatedAt"],
        "ledger": ledger,
        "depositHistory": deposit_history,
        "ui": ui,
        "tasks": tasks,
        "archive": archive,
    }
    return JSONResponse(content=payload, headers=SECURITY_HEADERS)


class ClientDoneRequest(BaseModel):
    done: bool
    comment: str | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def validate_request(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "done" in data and not data["done"] and "comment" in data:
            raise ValueError("comment is not allowed when done is false")
        if "comment" in data and data["comment"] is not None:
            if not isinstance(data["comment"], str):
                raise ValueError("comment must be a string")
            cleaned = "".join(ch for ch in data["comment"] if ch == "\n" or unicodedata.category(ch) != "Cc")
            cleaned = cleaned.strip()
            if len(cleaned) > 500:
                raise ValueError("comment exceeds 500 characters")
        return data


MAX_CLIENT_DONE_BODY_BYTES = 4096


async def _check_client_done_body_size(request: Request) -> None:
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > MAX_CLIENT_DONE_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="request body too large",
                    headers=SECURITY_HEADERS,
                )
        except ValueError:
            pass
    raw_body = await request.body()
    if len(raw_body) > MAX_CLIENT_DONE_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail="request body too large",
            headers=SECURITY_HEADERS,
        )


_client_done_rate_limits: dict[int, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW = 600.0  # 10 minutes
RATE_LIMIT_MAX_WRITES = 30


def _check_client_rate_limit(board_id: int) -> bool:
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    history = _client_done_rate_limits[board_id]
    _client_done_rate_limits[board_id] = [ts for ts in history if ts > cutoff]
    if len(_client_done_rate_limits[board_id]) >= RATE_LIMIT_MAX_WRITES:
        return False
    _client_done_rate_limits[board_id].append(now)
    return True


@router.post(
    "/{board_slug}/b/{token}/tasks/{task_id}/done",
    dependencies=[Depends(_check_client_done_body_size)],
)
def mark_client_done(
    board_slug: str,
    token: str,
    task_id: str,
    req: ClientDoneRequest,
) -> Response:
    board = _verify_access(board_slug, token)
    if not board:
        return _not_found_response()

    task = store.get_task(task_id)
    if (
        not task
        or task["boardId"] != board["id"]
        or not task["clientVisible"]
        or task["archivedAt"] is not None
        or task["status"] != "waiting"
    ):
        return _not_found_response()

    if not _check_client_rate_limit(board["id"]):
        return Response(
            content=b'{"detail":"too many requests"}',
            status_code=429,
            media_type="application/json",
            headers=SECURITY_HEADERS,
        )

    comment_provided = "comment" in req.model_fields_set
    try:
        res = store.set_task_client_done(
            board["id"],
            task_id,
            req.done,
            comment=req.comment,
            comment_provided=comment_provided,
            actor="client",
        )
    except ValueError as e:
        return Response(
            content=json.dumps({"detail": str(e)}).encode("utf-8"),
            status_code=422,
            media_type="application/json",
            headers=SECURITY_HEADERS,
        )
    return JSONResponse(content=res, headers=SECURITY_HEADERS)
