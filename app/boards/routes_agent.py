"""Agent API routes for boards (lane token auth via X-Bridge-Token)."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..routes_bridge import _auth_lane
from . import store

router = APIRouter()


class TaskCreateRequest(BaseModel):
    status: str = Field(default="backlog")
    grp: str = Field(default="")
    source: str = Field(default="")
    source_message_id: int | None = Field(default=None, alias="sourceMessageId")
    pay: str = Field(default="estimate")
    price: float = Field(default=0.0)
    hours: float | None = Field(default=None)
    client_visible: bool = Field(default=True, alias="clientVisible")
    title_i18n: dict[str, str] = Field(alias="titleI18n")
    client_note_i18n: dict[str, str] = Field(default_factory=dict, alias="clientNoteI18n")
    internal_note: str = Field(default="", alias="internalNote")

    model_config = {"populate_by_name": True}


class TaskUpdateRequest(BaseModel):
    status: str | None = Field(default=None)
    position: float | None = Field(default=None)
    grp: str | None = Field(default=None)
    source: str | None = Field(default=None)
    source_message_id: int | None = Field(default=None, alias="sourceMessageId")
    pay: str | None = Field(default=None)
    price: float | None = Field(default=None)
    hours: float | None = Field(default=None)
    client_visible: bool | None = Field(default=None, alias="clientVisible")
    title_i18n: dict[str, str] | None = Field(default=None, alias="titleI18n")
    client_note_i18n: dict[str, str] | None = Field(default=None, alias="clientNoteI18n")
    internal_note: str | None = Field(default=None, alias="internalNote")
    archived: bool | None = Field(default=None)
    client_done: bool | None = Field(default=None, alias="clientDone")
    version: int | None = Field(default=None)

    model_config = {"populate_by_name": True}


class BoardStatusRequest(BaseModel):
    status_i18n: dict[str, str] = Field(default_factory=dict, alias="statusI18n")

    model_config = {"populate_by_name": True}


def _get_agent_board(lane_slug: str, token: str, board_slug: str | None = None) -> tuple[dict, dict]:
    lane = _auth_lane(lane_slug, token)
    chat_id = (lane.get("default_chat_id") or "").strip()
    if not chat_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="lane is not bound to a chat yet — bind it to a Telegram chat in the admin panel",
        )

    boards = store.get_boards_for_chat(chat_id)
    if not boards:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no board found for this chat",
        )

    if board_slug:
        slug = board_slug.strip().lower()
        board = next((b for b in boards if b["slug"] == slug), None)
        if not board:
            # 404 to avoid revealing boards belonging to other chats
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"board '{board_slug}' not found for this chat",
            )
    else:
        board = boards[0]

    return lane, board


@router.get("/{lane_slug}/board")
def get_agent_board(
    lane_slug: str,
    x_bridge_token: str = Header(default="", alias="X-Bridge-Token"),
    board: str | None = Query(default=None),
    archived: str | None = Query(default=None),
) -> dict:
    lane, b = _get_agent_board(lane_slug, x_bridge_token, board_slug=board)
    bid = b["id"]
    ledger_derived = store.derive_ledger(bid)
    ledger_entries = store.list_ledger(bid)
    columns = store.get_columns_summary(bid)
    tasks = store.list_tasks(bid, archived=archived)

    return {
        "board": b,
        "ledger": {**ledger_derived, "entries": ledger_entries},
        "columns": columns,
        "tasks": tasks,
    }


@router.post("/{lane_slug}/board/tasks", status_code=status.HTTP_201_CREATED)
def create_agent_task(
    lane_slug: str,
    req: TaskCreateRequest,
    x_bridge_token: str = Header(default="", alias="X-Bridge-Token"),
    board: str | None = Query(default=None),
) -> dict:
    lane, b = _get_agent_board(lane_slug, x_bridge_token, board_slug=board)
    try:
        task = store.create_task(
            board_id=b["id"],
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
            actor=lane_slug,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return task


@router.patch("/{lane_slug}/board/tasks/{task_id}")
def update_agent_task(
    lane_slug: str,
    task_id: str,
    req: TaskUpdateRequest,
    x_bridge_token: str = Header(default="", alias="X-Bridge-Token"),
) -> Any:
    lane = _auth_lane(lane_slug, x_bridge_token)
    chat_id = (lane.get("default_chat_id") or "").strip()
    if not chat_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="lane is not bound to a chat yet — bind it to a Telegram chat in the admin panel",
        )

    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")

    board = store.get_board(task["boardId"])
    if not board or board["chatId"] != chat_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")

    set_source_message_id = "sourceMessageId" in req.model_fields_set or "source_message_id" in req.model_fields_set
    set_hours = "hours" in req.model_fields_set

    if req.client_done is True:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="agents cannot set clientDone, only clear it",
        )

    try:
        updated = store.update_task(
            board_id=board["id"],
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
            actor=lane_slug,
        )
    except store.VersionConflictError as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "version conflict", "task": e.current_task},
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    return updated


@router.put("/{lane_slug}/board/status")
def update_agent_board_status(
    lane_slug: str,
    req: BoardStatusRequest,
    x_bridge_token: str = Header(default="", alias="X-Bridge-Token"),
    board: str | None = Query(default=None),
) -> dict:
    lane, b = _get_agent_board(lane_slug, x_bridge_token, board_slug=board)
    try:
        updated = store.set_board_status(
            board_id=b["id"],
            status_i18n=req.status_i18n,
            actor=lane_slug,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return updated


@router.get("/{lane_slug}/board/events")
def get_agent_board_events(
    lane_slug: str,
    x_bridge_token: str = Header(default="", alias="X-Bridge-Token"),
    board: str | None = Query(default=None),
    since: int = Query(default=0),
    limit: int = Query(default=100),
) -> list[dict]:
    lane, b = _get_agent_board(lane_slug, x_bridge_token, board_slug=board)
    return store.list_events(b["id"], since=since, limit=limit)
