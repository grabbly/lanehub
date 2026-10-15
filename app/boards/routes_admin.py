"""Admin API routes for boards (cookie session auth via require_admin)."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import db
from ..routes_admin import require_admin
from . import store
from .routes_agent import TaskCreateRequest, TaskUpdateRequest

router = APIRouter(prefix="/admin/api/boards", dependencies=[Depends(require_admin)])


class BoardCreateRequest(BaseModel):
    chat_id: str = Field(alias="chatId")
    slug: str
    title: str = Field(default="")
    team_lang: str = Field(default="en", alias="teamLang")
    client_lang: str = Field(default="en", alias="clientLang")
    currency: str = Field(default="zł")
    rate: float | None = Field(default=None)

    model_config = {"populate_by_name": True}


class BoardUpdateRequest(BaseModel):
    chat_id: str | None = Field(default=None, alias="chatId")
    title: str | None = Field(default=None)
    team_lang: str | None = Field(default=None, alias="teamLang")
    client_lang: str | None = Field(default=None, alias="clientLang")
    currency: str | None = Field(default=None)
    rate: float | None = Field(default=None)
    status_i18n: dict[str, str] | None = Field(default=None, alias="statusI18n")

    model_config = {"populate_by_name": True}


class LedgerCreateRequest(BaseModel):
    kind: str
    amount: float
    at: str
    note: str = Field(default="")


class LedgerUpdateRequest(BaseModel):
    kind: str | None = Field(default=None)
    amount: float | None = Field(default=None)
    at: str | None = Field(default=None)
    note: str | None = Field(default=None)


def _board_or_404(board_id: int) -> dict:
    b = store.get_board(board_id)
    if not b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"board {board_id} not found")
    return b


# ---------------------------------------------------------------------------
# Board Endpoints
# ---------------------------------------------------------------------------

@router.get("")
def list_boards() -> list[dict]:
    boards = store.list_boards()
    result = []
    for b in boards:
        bid = b["id"]
        ledger = store.derive_ledger(bid)
        cols = store.get_columns_summary(bid)
        total_tasks = sum(c["count"] for c in cols.values())
        result.append({
            **b,
            "ledger": ledger,
            "taskCount": total_tasks,
            "archivedCount": store.count_archived_tasks(bid),
            "columns": cols,
        })
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
def create_board(req: BoardCreateRequest) -> dict:
    slug = req.slug.strip().lower()
    if not store.SLUG_RE.match(slug):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid slug '{slug}' — must match [a-z0-9][a-z0-9_-]{{0,31}}",
        )
    if slug in db.RESERVED_SLUGS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"slug '{slug}' is reserved",
        )

    # Check clash with lanes
    existing_lane = db.get_lane(slug)
    if existing_lane:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"slug '{slug}' clashes with an existing lane",
        )

    try:
        board, client_token = store.create_board(
            chat_id=req.chat_id,
            slug=slug,
            title=req.title,
            team_lang=req.team_lang,
            client_lang=req.client_lang,
            currency=req.currency,
            rate=req.rate,
            actor="admin",
        )
    except KeyError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    return {
        **board,
        "clientToken": client_token,
        "clientPath": f"/{slug}/b/{client_token}/",
    }


@router.get("/{board_id}")
def get_board(board_id: int, archived: str | None = Query(default=None)) -> dict:
    b = _board_or_404(board_id)
    ledger_derived = store.derive_ledger(board_id)
    ledger_entries = store.list_ledger(board_id)
    columns = store.get_columns_summary(board_id)
    tasks = store.list_tasks(board_id, archived=archived)

    return {
        **b,
        "ledger": {**ledger_derived, "entries": ledger_entries},
        "columns": columns,
        "tasks": tasks,
        "archivedCount": store.count_archived_tasks(board_id),
    }


@router.patch("/{board_id}")
def update_board(board_id: int, req: BoardUpdateRequest) -> dict:
    _board_or_404(board_id)
    try:
        updated = store.update_board(
            board_id=board_id,
            chat_id=req.chat_id,
            title=req.title,
            team_lang=req.team_lang,
            client_lang=req.client_lang,
            currency=req.currency,
            rate=req.rate,
            status_i18n=req.status_i18n,
            actor="admin",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return updated


@router.delete("/{board_id}")
def delete_board(board_id: int) -> dict:
    _board_or_404(board_id)
    store.delete_board(board_id, actor="admin")
    return {"deleted": True, "boardId": board_id}


@router.post("/{board_id}/rotate-client-token")
def rotate_token(board_id: int) -> dict:
    b = _board_or_404(board_id)
    new_token = store.rotate_client_token(board_id, actor="admin")
    return {
        "clientToken": new_token,
        "clientPath": f"/{b['slug']}/b/{new_token}/",
    }


# ---------------------------------------------------------------------------
# Tasks Endpoints
# ---------------------------------------------------------------------------

@router.get("/{board_id}/tasks")
def list_tasks(board_id: int, archived: str | None = Query(default=None)) -> list[dict]:
    _board_or_404(board_id)
    return store.list_tasks(board_id, archived=archived)


@router.post("/{board_id}/tasks", status_code=status.HTTP_201_CREATED)
def create_task(board_id: int, req: TaskCreateRequest) -> dict:
    _board_or_404(board_id)
    try:
        task = store.create_task(
            board_id=board_id,
            title_i18n=req.title_i18n,
            status=req.status,
            grp=req.grp,
            source=req.source,
            source_message_id=req.source_message_id,
            pay=req.pay,
            price=req.price,
            hours=req.hours,
            client_visible=req.client_visible,
            client_note_i18n=req.client_note_i18n,
            internal_note=req.internal_note,
            actor="admin",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return task


@router.patch("/{board_id}/tasks/{task_id}")
def update_task(board_id: int, task_id: str, req: TaskUpdateRequest) -> Any:
    _board_or_404(board_id)
    t = store.get_task(task_id)
    if not t or t["boardId"] != board_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")

    set_source_message_id = "sourceMessageId" in req.model_fields_set or "source_message_id" in req.model_fields_set
    set_hours = "hours" in req.model_fields_set

    try:
        updated = store.update_task(
            board_id=board_id,
            task_id=task_id,
            status=req.status,
            position=req.position,
            grp=req.grp,
            source=req.source,
            source_message_id=req.source_message_id,
            set_source_message_id=set_source_message_id,
            pay=req.pay,
            price=req.price,
            hours=req.hours,
            set_hours=set_hours,
            client_visible=req.client_visible,
            title_i18n=req.title_i18n,
            client_note_i18n=req.client_note_i18n,
            internal_note=req.internal_note,
            archived=req.archived,
            client_done=req.client_done,
            version=req.version,
            actor="admin",
        )
    except store.VersionConflictError as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "version conflict", "task": e.current_task},
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    return updated


@router.delete("/{board_id}/tasks/{task_id}")
def delete_task(board_id: int, task_id: str) -> dict:
    _board_or_404(board_id)
    t = store.get_task(task_id)
    if not t or t["boardId"] != board_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    store.delete_task(board_id, task_id, actor="admin")
    return {"deleted": True, "taskId": task_id}


# ---------------------------------------------------------------------------
# Ledger Endpoints
# ---------------------------------------------------------------------------

@router.get("/{board_id}/ledger")
def list_ledger(board_id: int) -> list[dict]:
    _board_or_404(board_id)
    return store.list_ledger(board_id)


@router.post("/{board_id}/ledger", status_code=status.HTTP_201_CREATED)
def create_ledger_entry(board_id: int, req: LedgerCreateRequest) -> dict:
    _board_or_404(board_id)
    try:
        entry = store.create_ledger_entry(
            board_id=board_id,
            kind=req.kind,
            amount=req.amount,
            at=req.at,
            note=req.note,
            actor="admin",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return entry


@router.patch("/{board_id}/ledger/{entry_id}")
def update_ledger_entry(board_id: int, entry_id: int, req: LedgerUpdateRequest) -> dict:
    _board_or_404(board_id)
    e = store.get_ledger_entry(entry_id)
    if not e or e["boardId"] != board_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ledger entry not found")
    try:
        updated = store.update_ledger_entry(
            board_id=board_id,
            entry_id=entry_id,
            kind=req.kind,
            amount=req.amount,
            note=req.note,
            at=req.at,
            actor="admin",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return updated


@router.delete("/{board_id}/ledger/{entry_id}")
def delete_ledger_entry(board_id: int, entry_id: int) -> dict:
    _board_or_404(board_id)
    e = store.get_ledger_entry(entry_id)
    if not e or e["boardId"] != board_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ledger entry not found")
    store.delete_ledger_entry(board_id, entry_id, actor="admin")
    return {"deleted": True, "entryId": entry_id}


# ---------------------------------------------------------------------------
# Events Endpoint
# ---------------------------------------------------------------------------

@router.get("/{board_id}/events")
def list_events(
    board_id: int,
    since: int = Query(default=0),
    limit: int = Query(default=100),
) -> list[dict]:
    _board_or_404(board_id)
    return store.list_events(board_id, since=since, limit=limit)


# ---------------------------------------------------------------------------
# Import Endpoint
# ---------------------------------------------------------------------------

@router.post("/{board_id}/import")
def import_into_board(
    board_id: int,
    payload: Any,
    dry_run: bool = Query(default=False, alias="dryRun"),
) -> dict:
    b = _board_or_404(board_id)
    tasks_data = []
    ledger_entries = None

    if isinstance(payload, list):
        tasks_data = payload
    elif isinstance(payload, dict):
        tasks_data = payload.get("tasks", [])
        if "ledger" in payload and isinstance(payload["ledger"], list):
            ledger_entries = []
            for item in payload["ledger"]:
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    kind, amt, at_date = item[0], float(item[1]), item[2]
                    note = item[3] if len(item) > 3 else ""
                    ledger_entries.append((kind, amt, at_date, note))
                elif isinstance(item, dict):
                    ledger_entries.append((
                        item.get("kind", "payment"),
                        float(item.get("amount", 0.0)),
                        item.get("at", ""),
                        item.get("note", ""),
                    ))
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="payload must be a list of tasks or an object with 'tasks'",
        )

    try:
        return store.import_board(
            board_slug=b["slug"],
            tasks_data=tasks_data,
            ledger_entries=ledger_entries,
            dry_run=dry_run,
            actor="admin",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
