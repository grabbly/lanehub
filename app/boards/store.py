"""SQLite storage and domain logic for the Boards module."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets as pysecrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from .. import db
from ..config import settings

LANGS = ("en", "pl", "ru", "uk")
STATUSES = ("backlog", "queue", "doing", "waiting", "done")
PAYS = ("estimate", "deposit", "paid", "rework", "bonus", "separate")
LEDGER_KINDS = ("payment", "deposit", "refund")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

BOARDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    team_lang TEXT NOT NULL DEFAULT 'en',
    client_lang TEXT NOT NULL DEFAULT 'en',
    currency TEXT NOT NULL DEFAULT 'zł',
    rate REAL,
    status_i18n TEXT NOT NULL DEFAULT '{}',
    status_updated_at INTEGER,
    client_token_hash TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS board_tasks (
    id TEXT PRIMARY KEY,
    board_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('backlog','queue','doing','waiting','done')),
    position REAL NOT NULL DEFAULT 0,
    grp TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_message_id INTEGER,
    pay TEXT NOT NULL DEFAULT 'estimate' CHECK(pay IN ('estimate','deposit','paid','rework','bonus','separate')),
    price REAL NOT NULL DEFAULT 0,
    hours REAL,
    client_visible INTEGER NOT NULL DEFAULT 1,
    title_i18n TEXT NOT NULL DEFAULT '{}',
    client_note_i18n TEXT NOT NULL DEFAULT '{}',
    internal_note TEXT NOT NULL DEFAULT '',
    archived_at INTEGER,
    client_done_at INTEGER,
    version INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_board_tasks_order ON board_tasks(board_id, status, position);
CREATE INDEX IF NOT EXISTS idx_board_tasks_archived ON board_tasks(board_id, archived_at);
CREATE TABLE IF NOT EXISTS board_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('payment','deposit','refund')),
    amount REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS board_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id INTEGER NOT NULL,
    task_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    at INTEGER NOT NULL
);
"""

# Dummy hash for constant-time comparison when token or board is missing
DUMMY_HASH = hashlib.sha256(b"lanehub-dummy-token-for-constant-time").hexdigest()


def _ensure_board_columns(conn: sqlite3.Connection) -> None:
    """Idempotent column adds for boards tables (like db._ensure_columns)."""
    b_cols = {r["name"] for r in conn.execute("PRAGMA table_info(boards)").fetchall()}
    if b_cols:
        if "status_i18n" not in b_cols:
            conn.execute("ALTER TABLE boards ADD COLUMN status_i18n TEXT NOT NULL DEFAULT '{}'")
        if "status_updated_at" not in b_cols:
            conn.execute("ALTER TABLE boards ADD COLUMN status_updated_at INTEGER")

    t_cols = {r["name"] for r in conn.execute("PRAGMA table_info(board_tasks)").fetchall()}
    if t_cols:
        if "archived_at" not in t_cols:
            conn.execute("ALTER TABLE board_tasks ADD COLUMN archived_at INTEGER")
        if "client_done_at" not in t_cols:
            conn.execute("ALTER TABLE board_tasks ADD COLUMN client_done_at INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_board_tasks_archived ON board_tasks(board_id, archived_at)")


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    _ensure_board_columns(conn)
    conn.executescript(BOARDS_SCHEMA)
    return conn


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_client_token() -> tuple[str, str]:
    raw = pysecrets.token_urlsafe(16)
    return raw, hash_token(raw)


def verify_client_token(raw_token: str, stored_hash: str | None) -> bool:
    target_hash = stored_hash or DUMMY_HASH
    computed = hash_token(raw_token) if raw_token else DUMMY_HASH
    matched = hmac.compare_digest(computed, target_hash)
    return bool(matched and stored_hash)


def validate_i18n_map(data: Any, field_name: str = "i18n") -> dict[str, str]:
    """Ensure data is a dict and only contains allowed language keys."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{field_name} must be a JSON map")
    cleaned = {}
    for k, v in data.items():
        if k not in LANGS:
            raise ValueError(f"Invalid language '{k}' in {field_name}. Allowed: {LANGS}")
        cleaned[k] = str(v) if v is not None else ""
    return cleaned


def validate_status_i18n(data: Any, field_name: str = "statusI18n") -> dict[str, str]:
    """Ensure data is a dict with valid language keys and each value <= 300 chars."""
    cleaned = validate_i18n_map(data, field_name)
    for k, v in cleaned.items():
        if len(v) > 300:
            raise ValueError(f"Status text for '{k}' exceeds 300 characters")
    return {k: v.strip() for k, v in cleaned.items() if v and v.strip()}


def resolve_text(i18n_map: dict[str, str], lang: str, team_lang: str) -> str:
    """Resolve text for viewer lang L: map[L] if non-empty, else map[team_lang], else first non-empty, else ''."""
    if not i18n_map:
        return ""
    val = i18n_map.get(lang, "").strip()
    if val:
        return val
    team_val = i18n_map.get(team_lang, "").strip()
    if team_val:
        return team_val
    for k in LANGS:
        v = i18n_map.get(k, "").strip()
        if v:
            return v
    for v in i18n_map.values():
        if v and v.strip():
            return v.strip()
    return ""


def missing_langs(title_i18n: dict[str, str]) -> list[str]:
    """Return list of languages with empty or missing title."""
    return [lang for lang in LANGS if not (title_i18n.get(lang) and title_i18n.get(lang).strip())]


def record_event(
    conn: sqlite3.Connection,
    board_id: int,
    actor: str,
    action: str,
    task_id: str | None = None,
    payload: dict | None = None,
) -> int:
    now = int(time.time())
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO board_events (board_id, task_id, actor, action, payload_json, at) VALUES (?, ?, ?, ?, ?, ?)",
        (board_id, task_id, actor, action, payload_json, now),
    )
    return cur.lastrowid  # type: ignore


# ---------------------------------------------------------------------------
# Board CRUD
# ---------------------------------------------------------------------------

def create_board(
    chat_id: str,
    slug: str,
    title: str = "",
    team_lang: str = "en",
    client_lang: str = "en",
    currency: str = "zł",
    rate: float | None = None,
    actor: str = "admin",
) -> tuple[dict, str]:
    """Create board, returning (board_dict, client_token)."""
    slug = slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise ValueError(f"Invalid slug '{slug}'. Must match {SLUG_RE.pattern}")
    if slug in db.RESERVED_SLUGS:
        raise ValueError(f"Slug '{slug}' is reserved")
    if team_lang not in LANGS:
        raise ValueError(f"Invalid team_lang '{team_lang}'. Allowed: {LANGS}")
    if client_lang not in LANGS:
        raise ValueError(f"Invalid client_lang '{client_lang}'. Allowed: {LANGS}")

    now = int(time.time())
    raw_token, token_hash = generate_client_token()

    with _conn() as conn:
        # Check lane collision
        existing_lane = conn.execute("SELECT slug FROM lanes WHERE slug = ?", (slug,)).fetchone()
        if existing_lane:
            raise KeyError(f"Slug '{slug}' clashes with an existing lane")
        try:
            cur = conn.execute(
                """
                INSERT INTO boards (
                    chat_id, slug, title, team_lang, client_lang, currency, rate,
                    client_token_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(chat_id).strip(), slug, title.strip(), team_lang, client_lang, currency.strip(), rate, token_hash, now, now),
            )
            board_id = cur.lastrowid
        except sqlite3.IntegrityError as e:
            raise KeyError(f"Board with slug '{slug}' already exists") from e

        record_event(
            conn,
            board_id,
            actor=actor,
            action="board_created",
            payload={"slug": slug, "title": title, "chatId": str(chat_id)},
        )

    board = get_board(board_id)
    return board, raw_token


def set_client_token(board_id: int, raw_token: str, actor: str = "admin") -> None:
    """Set client token (used by importer)."""
    token_hash = hash_token(raw_token)
    now = int(time.time())
    with _conn() as conn:
        conn.execute(
            "UPDATE boards SET client_token_hash = ?, updated_at = ? WHERE id = ?",
            (token_hash, now, board_id),
        )
        record_event(conn, board_id, actor=actor, action="token_rotated", payload={})


def rotate_client_token(board_id: int, actor: str = "admin") -> str:
    """Generate and store a new client token, returning the raw token."""
    raw_token, token_hash = generate_client_token()
    now = int(time.time())
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE boards SET client_token_hash = ?, updated_at = ? WHERE id = ?",
            (token_hash, now, board_id),
        )
        if cur.rowcount == 0:
            raise LookupError(f"Board {board_id} not found")
        record_event(conn, board_id, actor=actor, action="token_rotated", payload={})
    return raw_token


def get_board(board_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()
        return _board_row_to_dict(row) if row else None


def get_board_by_slug(slug: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM boards WHERE slug = ?", (slug,)).fetchone()
        return _board_row_to_dict(row) if row else None


def get_boards_for_chat(chat_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM boards WHERE chat_id = ? ORDER BY id ASC", (str(chat_id).strip(),)).fetchall()
        return [_board_row_to_dict(r) for r in rows]


def list_boards() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM boards ORDER BY id ASC").fetchall()
        return [_board_row_to_dict(r) for r in rows]


def update_board(
    board_id: int,
    chat_id: str | None = None,
    title: str | None = None,
    team_lang: str | None = None,
    client_lang: str | None = None,
    currency: str | None = None,
    rate: float | None = None,
    status_i18n: dict[str, str] | None = None,
    actor: str = "admin",
) -> dict:
    now = int(time.time())
    updates = []
    params = []
    payload = {}

    if chat_id is not None:
        updates.append("chat_id = ?")
        params.append(str(chat_id).strip())
        payload["chatId"] = str(chat_id).strip()
    if title is not None:
        updates.append("title = ?")
        params.append(title.strip())
        payload["title"] = title.strip()
    if team_lang is not None:
        if team_lang not in LANGS:
            raise ValueError(f"Invalid team_lang '{team_lang}'. Allowed: {LANGS}")
        updates.append("team_lang = ?")
        params.append(team_lang)
        payload["teamLang"] = team_lang
    if client_lang is not None:
        if client_lang not in LANGS:
            raise ValueError(f"Invalid client_lang '{client_lang}'. Allowed: {LANGS}")
        updates.append("client_lang = ?")
        params.append(client_lang)
        payload["clientLang"] = client_lang
    if currency is not None:
        updates.append("currency = ?")
        params.append(currency.strip())
        payload["currency"] = currency.strip()
    if rate is not None:
        updates.append("rate = ?")
        params.append(rate)
        payload["rate"] = rate
    if status_i18n is not None:
        cleaned_status = validate_status_i18n(status_i18n)
        has_text = any(v.strip() for v in cleaned_status.values())
        updates.append("status_i18n = ?")
        params.append(json.dumps(cleaned_status, ensure_ascii=False))
        updates.append("status_updated_at = ?")
        params.append(now if has_text else None)
        payload["statusI18n"] = cleaned_status

    if not updates:
        b = get_board(board_id)
        if not b:
            raise LookupError(f"Board {board_id} not found")
        return b

    updates.append("updated_at = ?")
    params.append(now)
    params.append(board_id)

    with _conn() as conn:
        cur = conn.execute(f"UPDATE boards SET {', '.join(updates)} WHERE id = ?", params)
        if cur.rowcount == 0:
            raise LookupError(f"Board {board_id} not found")
        if status_i18n is not None:
            record_event(conn, board_id, actor=actor, action="status", payload={"statusI18n": payload["statusI18n"]})
        other_payload = {k: v for k, v in payload.items() if k != "statusI18n"}
        if other_payload or status_i18n is None:
            record_event(conn, board_id, actor=actor, action="board_updated", payload=payload)

    return get_board(board_id)


def set_board_status(board_id: int, status_i18n: dict[str, str], actor: str = "agent") -> dict:
    """Update or clear board status line and record 'status' event."""
    cleaned = validate_status_i18n(status_i18n)
    now = int(time.time())
    has_text = any(v.strip() for v in cleaned.values())
    status_updated_at = now if has_text else None
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE boards SET status_i18n = ?, status_updated_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps(cleaned, ensure_ascii=False), status_updated_at, now, board_id),
        )
        if cur.rowcount == 0:
            raise LookupError(f"Board {board_id} not found")
        record_event(
            conn,
            board_id,
            actor=actor,
            action="status",
            payload={"statusI18n": cleaned},
        )
    return get_board(board_id)


def delete_board(board_id: int, actor: str = "admin") -> bool:
    """Delete board and cascade-delete tasks, ledger, events."""
    with _conn() as conn:
        row = conn.execute("SELECT slug, title FROM boards WHERE id = ?", (board_id,)).fetchone()
        if not row:
            return False
        # Record event before deleting board
        record_event(
            conn,
            board_id,
            actor=actor,
            action="board_deleted",
            payload={"slug": row["slug"], "title": row["title"]},
        )
        conn.execute("DELETE FROM board_tasks WHERE board_id = ?", (board_id,))
        conn.execute("DELETE FROM board_ledger WHERE board_id = ?", (board_id,))
        conn.execute("DELETE FROM board_events WHERE board_id = ?", (board_id,))
        conn.execute("DELETE FROM boards WHERE id = ?", (board_id,))
    return True


def _board_row_to_dict(row: sqlite3.Row) -> dict:
    # Notice: NEVER include client_token_hash here
    keys = row.keys()
    status_i18n = json.loads(row["status_i18n"] or "{}") if "status_i18n" in keys else {}
    status_updated_at = row["status_updated_at"] if "status_updated_at" in keys else None
    return {
        "id": row["id"],
        "chatId": row["chat_id"],
        "slug": row["slug"],
        "title": row["title"],
        "teamLang": row["team_lang"],
        "clientLang": row["client_lang"],
        "currency": row["currency"],
        "rate": row["rate"],
        "statusI18n": status_i18n,
        "statusUpdatedAt": status_updated_at,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get_board_token_hash(board_id: int) -> str | None:
    with _conn() as conn:
        row = conn.execute("SELECT client_token_hash FROM boards WHERE id = ?", (board_id,)).fetchone()
        return row["client_token_hash"] if row else None


def get_board_client_updated_at(board_id: int) -> str:
    """Return max updated time across board, client-visible tasks, and ledger events as ISO 8601 UTC string."""
    with _conn() as conn:
        b_row = conn.execute("SELECT updated_at, status_updated_at FROM boards WHERE id = ?", (board_id,)).fetchone()
        t_row = conn.execute("SELECT MAX(updated_at) as max_t FROM board_tasks WHERE board_id = ? AND client_visible = 1", (board_id,)).fetchone()
        e_row = conn.execute("SELECT MAX(at) as max_e FROM board_events WHERE board_id = ?", (board_id,)).fetchone()

        candidates = [b_row["updated_at"] if b_row else 0]
        if b_row and b_row["status_updated_at"]:
            candidates.append(b_row["status_updated_at"])
        if t_row and t_row["max_t"]:
            candidates.append(t_row["max_t"])
        if e_row and e_row["max_e"]:
            candidates.append(e_row["max_e"])

        max_ts = max(candidates) if candidates else int(time.time())
        return datetime.fromtimestamp(max_ts, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Derived Ledger & Columns
# ---------------------------------------------------------------------------

def derive_ledger(board_id: int) -> dict:
    """Calculate derived ledger numbers: paid, deposit, allocated, remaining."""
    with _conn() as conn:
        rows = conn.execute("SELECT kind, amount FROM board_ledger WHERE board_id = ?", (board_id,)).fetchall()
        paid = 0.0
        deposit_sum = 0.0
        refund_sum = 0.0
        for r in rows:
            amt = float(r["amount"] or 0)
            if r["kind"] == "payment":
                paid += amt
            elif r["kind"] == "deposit":
                deposit_sum += amt
            elif r["kind"] == "refund":
                refund_sum += amt

        deposit = deposit_sum - refund_sum

        allocated_row = conn.execute(
            "SELECT COALESCE(SUM(price), 0.0) as alloc FROM board_tasks WHERE board_id = ? AND pay = 'deposit'",
            (board_id,),
        ).fetchone()
        allocated = float(allocated_row["alloc"] or 0.0)
        remaining = deposit - allocated

        return {
            "paid": paid,
            "deposit": deposit,
            "allocated": allocated,
            "remaining": remaining,
        }


def get_columns_summary(board_id: int) -> dict[str, dict[str, Any]]:
    """Return per-column count and sum (for pay in ('deposit','paid'))."""
    summary = {st: {"count": 0, "sum": 0.0} for st in STATUSES}
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT status,
                   COUNT(*) as cnt,
                   SUM(CASE WHEN pay IN ('deposit', 'paid') THEN price ELSE 0.0 END) as total_price
            FROM board_tasks
            WHERE board_id = ?
            GROUP BY status
            """,
            (board_id,),
        ).fetchall()
        for r in rows:
            st = r["status"]
            if st in summary:
                summary[st] = {
                    "count": int(r["cnt"] or 0),
                    "sum": float(r["total_price"] or 0.0),
                }
    return summary


# ---------------------------------------------------------------------------
# Tasks CRUD
# ---------------------------------------------------------------------------

def _task_row_to_dict(row: sqlite3.Row) -> dict:
    title_i18n = json.loads(row["title_i18n"] or "{}")
    client_note_i18n = json.loads(row["client_note_i18n"] or "{}")
    keys = row.keys()
    archived_at = row["archived_at"] if "archived_at" in keys else None
    client_done_at = row["client_done_at"] if "client_done_at" in keys else None
    return {
        "id": row["id"],
        "boardId": row["board_id"],
        "status": row["status"],
        "position": float(row["position"]),
        "grp": row["grp"],
        "source": row["source"],
        "sourceMessageId": row["source_message_id"],
        "pay": row["pay"],
        "price": float(row["price"]),
        "hours": float(row["hours"]) if row["hours"] is not None else None,
        "clientVisible": bool(row["client_visible"]),
        "titleI18n": title_i18n,
        "clientNoteI18n": client_note_i18n,
        "internalNote": row["internal_note"],
        "archivedAt": archived_at,
        "clientDoneAt": client_done_at,
        "version": row["version"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "updatedBy": row["updated_by"] if "updated_by" in keys else "",
        "missingLangs": missing_langs(title_i18n),
    }


def create_task(
    board_id: int,
    title_i18n: dict[str, str],
    status: str = "backlog",
    grp: str = "",
    source: str = "",
    source_message_id: int | None = None,
    pay: str = "estimate",
    price: float = 0.0,
    hours: float | None = None,
    client_visible: bool = True,
    client_note_i18n: dict[str, str] | None = None,
    internal_note: str = "",
    position: float | None = None,
    task_id: str | None = None,
    actor: str = "admin",
) -> dict:
    if status not in STATUSES:
        raise ValueError(f"Invalid status '{status}'. Allowed: {STATUSES}")
    if pay not in PAYS:
        raise ValueError(f"Invalid pay '{pay}'. Allowed: {PAYS}")

    cleaned_title = validate_i18n_map(title_i18n, "titleI18n")
    if not any(v.strip() for v in cleaned_title.values()):
        raise ValueError("titleI18n must have at least one non-empty value")

    cleaned_note = validate_i18n_map(client_note_i18n, "clientNoteI18n")
    tid = task_id.strip() if task_id else pysecrets.token_hex(6)
    now = int(time.time())

    with _conn() as conn:
        # Check board exists
        b = conn.execute("SELECT id FROM boards WHERE id = ?", (board_id,)).fetchone()
        if not b:
            raise LookupError(f"Board {board_id} not found")

        if position is None:
            max_row = conn.execute(
                "SELECT MAX(position) as max_pos FROM board_tasks WHERE board_id = ? AND status = ?",
                (board_id, status),
            ).fetchone()
            max_pos = max_row["max_pos"]
            position = float(max_pos) + 10.0 if max_pos is not None else 10.0

        conn.execute(
            """
            INSERT INTO board_tasks (
                id, board_id, status, position, grp, source, source_message_id,
                pay, price, hours, client_visible, title_i18n, client_note_i18n,
                internal_note, version, created_at, updated_at, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                tid,
                board_id,
                status,
                float(position),
                grp.strip(),
                source.strip(),
                source_message_id,
                pay,
                float(price),
                float(hours) if hours is not None else None,
                1 if client_visible else 0,
                json.dumps(cleaned_title, ensure_ascii=False),
                json.dumps(cleaned_note, ensure_ascii=False),
                internal_note,
                now,
                now,
                actor,
            ),
        )

        record_event(
            conn,
            board_id,
            actor=actor,
            action="task_created",
            task_id=tid,
            payload={"status": status, "position": position, "pay": pay, "price": price},
        )

    task = get_task(tid)
    return task


def get_task(task_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM board_tasks WHERE id = ?", (task_id,)).fetchone()
        return _task_row_to_dict(row) if row else None


def list_tasks(
    board_id: int,
    client_visible_only: bool = False,
    archived: str | bool | None = False,
) -> list[dict]:
    with _conn() as conn:
        sql = "SELECT * FROM board_tasks WHERE board_id = ?"
        params: list[Any] = [board_id]
        if client_visible_only:
            sql += " AND client_visible = 1"
        if archived == "1" or archived is True:
            sql += " AND archived_at IS NOT NULL"
        elif archived == "all":
            pass
        else:
            sql += " AND archived_at IS NULL"
        sql += " ORDER BY status ASC, position ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_task_row_to_dict(r) for r in rows]


def count_archived_tasks(board_id: int) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM board_tasks WHERE board_id = ? AND archived_at IS NOT NULL",
            (board_id,),
        ).fetchone()
        return int(row["cnt"] or 0) if row else 0


class VersionConflictError(Exception):
    def __init__(self, current_task: dict):
        super().__init__("Version conflict")
        self.current_task = current_task


def update_task(
    board_id: int,
    task_id: str,
    status: str | None = None,
    position: float | None = None,
    grp: str | None = None,
    source: str | None = None,
    source_message_id: int | None = None,
    set_source_message_id: bool = False,
    pay: str | None = None,
    price: float | None = None,
    hours: float | None = None,
    set_hours: bool = False,
    client_visible: bool | None = None,
    title_i18n: dict[str, str] | None = None,
    client_note_i18n: dict[str, str] | None = None,
    internal_note: str | None = None,
    archived: bool | None = None,
    client_done: bool | None = None,
    version: int | None = None,
    actor: str = "admin",
) -> dict:
    """Update task with optimistic concurrency. If version is provided and != current version, raises VersionConflictError."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM board_tasks WHERE id = ? AND board_id = ?", (task_id, board_id)).fetchone()
        if not row:
            raise LookupError(f"Task {task_id} not found on board {board_id}")

        current_task = _task_row_to_dict(row)
        if version is not None and version != row["version"]:
            raise VersionConflictError(current_task)

        updates = []
        params = []
        payload = {}

        if status is not None:
            if status not in STATUSES:
                raise ValueError(f"Invalid status '{status}'. Allowed: {STATUSES}")
            updates.append("status = ?")
            params.append(status)
            payload["status"] = status

        if position is not None:
            updates.append("position = ?")
            params.append(float(position))
            payload["position"] = float(position)

        if grp is not None:
            updates.append("grp = ?")
            params.append(grp.strip())
            payload["grp"] = grp.strip()

        if source is not None:
            updates.append("source = ?")
            params.append(source.strip())
            payload["source"] = source.strip()

        if set_source_message_id:
            updates.append("source_message_id = ?")
            params.append(source_message_id)
            payload["sourceMessageId"] = source_message_id

        if pay is not None:
            if pay not in PAYS:
                raise ValueError(f"Invalid pay '{pay}'. Allowed: {PAYS}")
            updates.append("pay = ?")
            params.append(pay)
            payload["pay"] = pay

        if price is not None:
            updates.append("price = ?")
            params.append(float(price))
            payload["price"] = float(price)

        if set_hours:
            updates.append("hours = ?")
            params.append(float(hours) if hours is not None else None)
            payload["hours"] = float(hours) if hours is not None else None

        if client_visible is not None:
            val = 1 if client_visible else 0
            updates.append("client_visible = ?")
            params.append(val)
            payload["clientVisible"] = bool(val)

        if title_i18n is not None:
            cleaned = validate_i18n_map(title_i18n, "titleI18n")
            if not any(v.strip() for v in cleaned.values()):
                raise ValueError("titleI18n must have at least one non-empty value")
            updates.append("title_i18n = ?")
            params.append(json.dumps(cleaned, ensure_ascii=False))
            payload["titleI18n"] = cleaned

        if client_note_i18n is not None:
            cleaned = validate_i18n_map(client_note_i18n, "clientNoteI18n")
            updates.append("client_note_i18n = ?")
            params.append(json.dumps(cleaned, ensure_ascii=False))
            payload["clientNoteI18n"] = cleaned

        if internal_note is not None:
            updates.append("internal_note = ?")
            params.append(internal_note)
            payload["internalNote"] = internal_note

        now = int(time.time())
        if archived is not None:
            if archived:
                updates.append("archived_at = ?")
                params.append(now)
                payload["archived"] = True
                payload["archivedAt"] = now
            else:
                updates.append("archived_at = NULL")
                payload["archived"] = False
                payload["archivedAt"] = None

        old_status = current_task["status"]
        effective_status = status if status is not None else old_status

        if client_done is not None:
            if client_done:
                if effective_status != "waiting":
                    raise ValueError("clientDone can only be set when task is in waiting status")
                updates.append("client_done_at = ?")
                params.append(now)
                payload["clientDone"] = True
                payload["clientDoneAt"] = now
            else:
                updates.append("client_done_at = NULL")
                payload["clientDone"] = False
                payload["clientDoneAt"] = None
        elif status is not None and status != old_status:
            # Reset rule: when a task leaves waiting (status changes) client_done_at is cleared automatically. Moving back to waiting starts clean.
            if current_task.get("clientDoneAt") is not None:
                updates.append("client_done_at = NULL")
                payload["clientDoneAt"] = None

        updates.append("version = version + 1")
        updates.append("updated_at = ?")
        params.append(now)
        updates.append("updated_by = ?")
        params.append(actor)

        params.extend([task_id, board_id])
        conn.execute(
            f"UPDATE board_tasks SET {', '.join(updates)} WHERE id = ? AND board_id = ?",
            params,
        )

        if archived is not None:
            action = "archived" if archived else "unarchived"
            record_event(
                conn,
                board_id,
                actor=actor,
                action=action,
                task_id=task_id,
                payload={"archivedAt": payload.get("archivedAt")},
            )

        other_payload = {k: v for k, v in payload.items() if k not in ("archived", "archivedAt")}
        if other_payload or archived is None:
            record_event(
                conn,
                board_id,
                actor=actor,
                action="task_updated",
                task_id=task_id,
                payload=payload,
            )

    return get_task(task_id)


def set_task_client_done(board_id: int, task_id: str, done: bool, actor: str = "client") -> dict:
    """Set or clear client_done_at for a task in waiting status."""
    now = int(time.time())
    client_done_at = now if done else None
    with _conn() as conn:
        row = conn.execute("SELECT * FROM board_tasks WHERE id = ? AND board_id = ?", (task_id, board_id)).fetchone()
        if not row:
            raise LookupError(f"Task {task_id} not found on board {board_id}")
        if row["status"] != "waiting":
            raise ValueError(f"Task {task_id} is not in waiting status")
        conn.execute(
            """
            UPDATE board_tasks
            SET client_done_at = ?, updated_at = ?, version = version + 1, updated_by = ?
            WHERE id = ? AND board_id = ?
            """,
            (client_done_at, now, actor, task_id, board_id),
        )
        record_event(
            conn,
            board_id,
            actor=actor,
            action="client_done" if done else "client_undone",
            task_id=task_id,
            payload={"clientDoneAt": client_done_at},
        )
    return {"id": task_id, "clientDoneAt": client_done_at}


def delete_task(board_id: int, task_id: str, actor: str = "admin") -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT id FROM board_tasks WHERE id = ? AND board_id = ?", (task_id, board_id)).fetchone()
        if not row:
            return False
        record_event(
            conn,
            board_id,
            actor=actor,
            action="task_deleted",
            task_id=task_id,
            payload={"id": task_id},
        )
        conn.execute("DELETE FROM board_tasks WHERE id = ? AND board_id = ?", (task_id, board_id))
    return True


# ---------------------------------------------------------------------------
# Ledger CRUD
# ---------------------------------------------------------------------------

def _ledger_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "boardId": row["board_id"],
        "kind": row["kind"],
        "amount": float(row["amount"]),
        "note": row["note"],
        "at": row["at"],
    }


def create_ledger_entry(
    board_id: int,
    kind: str,
    amount: float,
    at: str,
    note: str = "",
    actor: str = "admin",
) -> dict:
    if kind not in LEDGER_KINDS:
        raise ValueError(f"Invalid ledger kind '{kind}'. Allowed: {LEDGER_KINDS}")

    with _conn() as conn:
        b = conn.execute("SELECT id FROM boards WHERE id = ?", (board_id,)).fetchone()
        if not b:
            raise LookupError(f"Board {board_id} not found")

        cur = conn.execute(
            "INSERT INTO board_ledger (board_id, kind, amount, note, at) VALUES (?, ?, ?, ?, ?)",
            (board_id, kind, float(amount), note.strip(), at.strip()),
        )
        entry_id = cur.lastrowid
        record_event(
            conn,
            board_id,
            actor=actor,
            action="ledger_created",
            payload={"id": entry_id, "kind": kind, "amount": float(amount), "at": at.strip()},
        )

    return get_ledger_entry(entry_id)


def get_ledger_entry(entry_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM board_ledger WHERE id = ?", (entry_id,)).fetchone()
        return _ledger_row_to_dict(row) if row else None


def list_ledger(board_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM board_ledger WHERE board_id = ? ORDER BY at ASC, id ASC", (board_id,)).fetchall()
        return [_ledger_row_to_dict(r) for r in rows]


def update_ledger_entry(
    board_id: int,
    entry_id: int,
    kind: str | None = None,
    amount: float | None = None,
    note: str | None = None,
    at: str | None = None,
    actor: str = "admin",
) -> dict:
    updates = []
    params = []
    payload = {}

    if kind is not None:
        if kind not in LEDGER_KINDS:
            raise ValueError(f"Invalid ledger kind '{kind}'. Allowed: {LEDGER_KINDS}")
        updates.append("kind = ?")
        params.append(kind)
        payload["kind"] = kind
    if amount is not None:
        updates.append("amount = ?")
        params.append(float(amount))
        payload["amount"] = float(amount)
    if note is not None:
        updates.append("note = ?")
        params.append(note.strip())
        payload["note"] = note.strip()
    if at is not None:
        updates.append("at = ?")
        params.append(at.strip())
        payload["at"] = at.strip()

    if not updates:
        e = get_ledger_entry(entry_id)
        if not e:
            raise LookupError(f"Ledger entry {entry_id} not found")
        return e

    params.extend([entry_id, board_id])
    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE board_ledger SET {', '.join(updates)} WHERE id = ? AND board_id = ?",
            params,
        )
        if cur.rowcount == 0:
            raise LookupError(f"Ledger entry {entry_id} not found on board {board_id}")
        record_event(conn, board_id, actor=actor, action="ledger_updated", payload=payload)

    return get_ledger_entry(entry_id)


def delete_ledger_entry(board_id: int, entry_id: int, actor: str = "admin") -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT id FROM board_ledger WHERE id = ? AND board_id = ?", (entry_id, board_id)).fetchone()
        if not row:
            return False
        record_event(
            conn,
            board_id,
            actor=actor,
            action="ledger_deleted",
            payload={"id": entry_id},
        )
        conn.execute("DELETE FROM board_ledger WHERE id = ? AND board_id = ?", (entry_id, board_id))
    return True


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def list_events(board_id: int, since: int = 0, limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM board_events WHERE board_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (board_id, since, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "boardId": r["board_id"],
                "taskId": r["task_id"],
                "actor": r["actor"],
                "action": r["action"],
                "payload": json.loads(r["payload_json"] or "{}"),
                "at": r["at"],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Importer logic
# ---------------------------------------------------------------------------

def _parse_price(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_hours(val: Any, internal_note: str, warnings: list[str]) -> tuple[float | None, str]:
    if val is None or val == "":
        return None, internal_note
    if isinstance(val, (int, float)):
        return float(val), internal_note
    s = str(val).strip().replace(",", ".")
    try:
        return float(s), internal_note
    except ValueError:
        warnings.append(f"Unparseable hours '{val}', appended to internal_note")
        suffix = f"hours: {val}"
        new_note = f"{internal_note}\n{suffix}".strip() if internal_note else suffix
        return None, new_note


def import_board(
    board_slug: str,
    tasks_data: list[dict],
    ledger_entries: list[tuple[str, float, str, str]] | None = None,
    dry_run: bool = False,
    create_params: dict | None = None,
    client_token: str | None = None,
    actor: str = "importer",
) -> dict:
    """Import tasks and ledger into board. Idempotent on task ID. If dry_run, writes nothing."""
    warnings: list[str] = []
    created_tasks = 0
    updated_tasks = 0
    created_ledger = 0

    board = get_board_by_slug(board_slug)
    if not board:
        if create_params:
            if not dry_run:
                board, _ = create_board(
                    chat_id=create_params.get("chat_id", ""),
                    slug=board_slug,
                    title=create_params.get("title", ""),
                    team_lang=create_params.get("team_lang", "ru"),
                    client_lang=create_params.get("client_lang", "uk"),
                    currency=create_params.get("currency", "zł"),
                    rate=create_params.get("rate", 40.0),
                    actor=actor,
                )
            else:
                board = {"id": -1, "slug": board_slug}
        else:
            raise LookupError(f"Board '{board_slug}' not found and no create_params provided")

    board_id = board["id"]

    parsed_tasks = []
    for item in tasks_data:
        # Item may be {"id": "...", "data": {...}} or bare {...}
        if "data" in item and isinstance(item["data"], dict):
            raw_id = item.get("id")
            data = item["data"]
        else:
            raw_id = item.get("id")
            data = item

        tid = str(raw_id).strip() if raw_id else (str(data.get("id")).strip() if data.get("id") else pysecrets.token_hex(6))

        # Field mapping
        title_ru = data.get("title", "")
        title_uk = data.get("title_uk", "")
        title_i18n = data.get("title_i18n") or {}
        if not isinstance(title_i18n, dict):
            title_i18n = {}
        if title_ru and "ru" not in title_i18n:
            title_i18n["ru"] = str(title_ru)
        if title_uk and "uk" not in title_i18n:
            title_i18n["uk"] = str(title_uk)

        # Default fallback if title is still empty
        if not any(v.strip() for v in title_i18n.values()):
            title_i18n["ru"] = str(title_ru or tid)

        client_val = data.get("client", data.get("client_visible", True))
        client_visible = False if client_val is False or client_val == 0 or client_val == "false" else True

        internal_note = str(data.get("note", data.get("internal_note", "")))
        note_uk = data.get("note_uk", "")
        client_note_i18n = data.get("client_note_i18n") or {}
        if not isinstance(client_note_i18n, dict):
            client_note_i18n = {}
        if note_uk and "uk" not in client_note_i18n:
            client_note_i18n["uk"] = str(note_uk)

        order_val = data.get("order", data.get("position", 0.0))
        try:
            position = float(order_val)
        except (ValueError, TypeError):
            position = 0.0

        grp = str(data.get("group", data.get("grp", "")))
        source = str(data.get("source", ""))
        source_msg_id = data.get("source_message_id")
        try:
            source_message_id = int(source_msg_id) if source_msg_id is not None else None
        except (ValueError, TypeError):
            source_message_id = None

        status = str(data.get("status", "backlog")).strip().lower()
        if status not in STATUSES:
            warnings.append(f"Task {tid}: unknown status '{status}' mapped to 'backlog'")
            status = "backlog"

        pay = str(data.get("pay", "estimate")).strip().lower()
        if pay == "warranty":
            pay = "rework"
        elif pay not in PAYS:
            warnings.append(f"Task {tid}: unknown pay '{pay}' mapped to 'estimate'")
            pay = "estimate"

        price = _parse_price(data.get("price"))
        hours, internal_note = _parse_hours(data.get("hours"), internal_note, warnings)

        parsed_tasks.append({
            "id": tid,
            "status": status,
            "position": position,
            "grp": grp,
            "source": source,
            "sourceMessageId": source_message_id,
            "pay": pay,
            "price": price,
            "hours": hours,
            "clientVisible": client_visible,
            "titleI18n": title_i18n,
            "clientNoteI18n": client_note_i18n,
            "internalNote": internal_note,
        })

    # Task ids are global (PRIMARY KEY), so an id another board already holds
    # would fail the INSERT halfway through; refuse the whole import up front.
    with _conn() as conn:
        clashes = [
            pt["id"] for pt in parsed_tasks
            if conn.execute(
                "SELECT 1 FROM board_tasks WHERE id = ? AND board_id != ?", (pt["id"], board_id)
            ).fetchone()
        ]
    if clashes:
        raise ValueError(f"task ids already used on another board: {', '.join(clashes[:10])}")

    if not dry_run:
        now = int(time.time())
        with _conn() as conn:
            for pt in parsed_tasks:
                existing = conn.execute(
                    "SELECT id FROM board_tasks WHERE id = ? AND board_id = ?",
                    (pt["id"], board_id),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE board_tasks SET
                            status = ?, position = ?, grp = ?, source = ?, source_message_id = ?,
                            pay = ?, price = ?, hours = ?, client_visible = ?, title_i18n = ?,
                            client_note_i18n = ?, internal_note = ?, version = version + 1,
                            updated_at = ?, updated_by = ?
                        WHERE id = ? AND board_id = ?
                        """,
                        (
                            pt["status"],
                            pt["position"],
                            pt["grp"],
                            pt["source"],
                            pt["sourceMessageId"],
                            pt["pay"],
                            pt["price"],
                            pt["hours"],
                            1 if pt["clientVisible"] else 0,
                            json.dumps(pt["titleI18n"], ensure_ascii=False),
                            json.dumps(pt["clientNoteI18n"], ensure_ascii=False),
                            pt["internalNote"],
                            now,
                            actor,
                            pt["id"],
                            board_id,
                        ),
                    )
                    updated_tasks += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO board_tasks (
                            id, board_id, status, position, grp, source, source_message_id,
                            pay, price, hours, client_visible, title_i18n, client_note_i18n,
                            internal_note, version, created_at, updated_at, updated_by
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (
                            pt["id"],
                            board_id,
                            pt["status"],
                            pt["position"],
                            pt["grp"],
                            pt["source"],
                            pt["sourceMessageId"],
                            pt["pay"],
                            pt["price"],
                            pt["hours"],
                            1 if pt["clientVisible"] else 0,
                            json.dumps(pt["titleI18n"], ensure_ascii=False),
                            json.dumps(pt["clientNoteI18n"], ensure_ascii=False),
                            pt["internalNote"],
                            now,
                            now,
                            actor,
                        ),
                    )
                    created_tasks += 1

            if ledger_entries:
                for kind, amount, at, note in ledger_entries:
                    conn.execute(
                        "INSERT INTO board_ledger (board_id, kind, amount, note, at) VALUES (?, ?, ?, ?, ?)",
                        (board_id, kind, amount, note, at),
                    )
                    created_ledger += 1

            if client_token:
                token_hash = hash_token(client_token)
                conn.execute(
                    "UPDATE boards SET client_token_hash = ?, updated_at = ? WHERE id = ?",
                    (token_hash, now, board_id),
                )

            record_event(
                conn,
                board_id,
                actor=actor,
                action="board_imported",
                payload={
                    "createdTasks": created_tasks,
                    "updatedTasks": updated_tasks,
                    "createdLedger": created_ledger,
                },
            )
    else:
        # In dry run, count would-be created vs updated
        with _conn() as conn:
            for pt in parsed_tasks:
                existing = conn.execute(
                    "SELECT id FROM board_tasks WHERE id = ? AND board_id = ?",
                    (pt["id"], board_id),
                ).fetchone() if board_id != -1 else None
                if existing:
                    updated_tasks += 1
                else:
                    created_tasks += 1
        created_ledger = len(ledger_entries) if ledger_entries else 0

    return {
        "boardSlug": board_slug,
        "createdTasks": created_tasks,
        "updatedTasks": updated_tasks,
        "createdLedger": created_ledger,
        "warnings": warnings,
        "dryRun": dry_run,
    }
