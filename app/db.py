"""SQLite storage for LaneHub.

One database holds everything: lanes (bot identities + API keys), the merged
message history of every lane, per-lane poller state, and the chats each bot
has seen. Short-lived connections per operation keep things simple and safe
across asyncio tasks; WAL mode keeps readers and the writer out of each
other's way.
"""
from __future__ import annotations

import secrets
import sqlite3
import time

from .config import settings

# Outgoing messages get synthetic update_ids in a high namespace so they never
# collide with real Telegram update_ids (32-bit ints) and sort after them.
OUTGOING_BASE = 1_000_000_000_000_000

RESERVED_SLUGS = {"admin", "portal", "api", "health", "version", "static", "assets", "docs", "favicon.ico"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS lanes (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    bot_token TEXT NOT NULL,
    bot_username TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL UNIQUE,
    webhook_secret TEXT NOT NULL,
    default_chat_id TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    lane_slug TEXT NOT NULL,
    update_id INTEGER NOT NULL,
    message_id INTEGER,
    chat_id INTEGER,
    chat_title TEXT,
    from_user TEXT,
    text TEXT,
    date INTEGER NOT NULL DEFAULT 0,
    is_outgoing INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lane_slug, update_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);
CREATE TABLE IF NOT EXISTS lane_state (
    lane_slug TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (lane_slug, key)
);
CREATE TABLE IF NOT EXISTS seen_chats (
    lane_slug TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    title TEXT,
    last_date INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lane_slug, chat_id)
);
CREATE TABLE IF NOT EXISTS hub_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS members (
    email TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    lane_slug TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    last_login INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lane_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lane_slug TEXT NOT NULL,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lane_logs ON lane_logs(lane_slug, id);
CREATE TABLE IF NOT EXISTS operator_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lane_slug TEXT NOT NULL,
    text TEXT NOT NULL,
    from_user TEXT,
    ts INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_operator_inbox ON operator_inbox(lane_slug, status, id);
"""

# Keep at most this many log lines per lane (a rolling watcher-activity buffer).
LANE_LOG_CAP = 300


def connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotent column adds for tables that predate a field (SQLite has no
    ADD COLUMN IF NOT EXISTS)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(lane_logs)")}
    if "cost_usd" not in cols:
        conn.execute("ALTER TABLE lane_logs ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0")


def new_api_key() -> str:
    return secrets.token_urlsafe(32)


def new_webhook_secret() -> str:
    return secrets.token_urlsafe(24)


# --- hub state -----------------------------------------------------------


def get_hub_state(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM hub_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_hub_state(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO hub_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def session_secret() -> str:
    secret = get_hub_state("session_secret")
    if not secret:
        secret = secrets.token_urlsafe(32)
        set_hub_state("session_secret", secret)
    return secret


# --- members ---------------------------------------------------------------


def hash_password(password: str) -> str:
    import hashlib

    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"{salt}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    import hashlib

    try:
        salt, expected = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    except (ValueError,):
        return False
    return secrets.compare_digest(dk.hex(), expected)


def create_member(email: str, name: str, password_hash: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO members(email, name, password_hash, created_at) VALUES(?, ?, ?, ?)",
            (email, name, password_hash, int(time.time())),
        )


def get_member(email: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM members WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def list_members() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM members ORDER BY created_at")]


def update_member(email: str, fields: dict) -> None:
    allowed = {"name", "password_hash", "lane_slug", "last_login"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if updates:
        cols = ", ".join(f"{k} = ?" for k in updates)
        with connect() as conn:
            conn.execute(f"UPDATE members SET {cols} WHERE email = ?", (*updates.values(), email))


def delete_member(email: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM members WHERE email = ?", (email,))


# --- lanes ---------------------------------------------------------------


def lane_to_dict(row: sqlite3.Row, include_secrets: bool = False) -> dict:
    d = {
        "slug": row["slug"],
        "title": row["title"],
        "botUsername": row["bot_username"],
        "defaultChatId": row["default_chat_id"],
        "enabled": bool(row["enabled"]),
        "createdAt": row["created_at"],
    }
    if include_secrets:
        d["apiKey"] = row["api_key"]
    return d


def create_lane(slug: str, title: str, bot_token: str, bot_username: str, default_chat_id: str) -> dict:
    with connect() as conn:
        conn.execute(
            "INSERT INTO lanes(slug, title, bot_token, bot_username, api_key, webhook_secret, "
            "default_chat_id, enabled, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                slug,
                title,
                bot_token,
                bot_username,
                new_api_key(),
                new_webhook_secret(),
                default_chat_id,
                int(time.time()),
            ),
        )
    return get_lane(slug)  # type: ignore[return-value]


def get_lane(slug: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM lanes WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None


def list_lanes() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM lanes ORDER BY created_at")]


def update_lane(slug: str, fields: dict) -> dict | None:
    allowed = {"title", "bot_token", "bot_username", "default_chat_id", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if updates:
        cols = ", ".join(f"{k} = ?" for k in updates)
        with connect() as conn:
            conn.execute(f"UPDATE lanes SET {cols} WHERE slug = ?", (*updates.values(), slug))
    return get_lane(slug)


def rotate_lane_key(slug: str) -> str:
    key = new_api_key()
    with connect() as conn:
        conn.execute("UPDATE lanes SET api_key = ? WHERE slug = ?", (key, slug))
    return key


def delete_lane(slug: str) -> None:
    # Message history is intentionally kept — it is the team's chat archive.
    with connect() as conn:
        conn.execute("DELETE FROM lanes WHERE slug = ?", (slug,))
        conn.execute("DELETE FROM lane_state WHERE lane_slug = ?", (slug,))
        conn.execute("DELETE FROM lane_logs WHERE lane_slug = ?", (slug,))


# --- lane state (poll offsets) -------------------------------------------


def get_lane_state(slug: str, key: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM lane_state WHERE lane_slug = ? AND key = ?", (slug, key)
        ).fetchone()
        return row["value"] if row else None


def set_lane_state(slug: str, key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO lane_state(lane_slug, key, value) VALUES(?, ?, ?) "
            "ON CONFLICT(lane_slug, key) DO UPDATE SET value = excluded.value",
            (slug, key, value),
        )


# --- lane logs (watcher activity, rolling per lane) ----------------------


def add_lane_log(slug: str, level: str, message: str, ts: int, cost_usd: float = 0.0) -> None:
    """Append one watcher log line for a lane and trim to the last LANE_LOG_CAP."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO lane_logs(lane_slug, ts, level, message, cost_usd) VALUES(?, ?, ?, ?, ?)",
            (slug, ts, (level or "info")[:16], message[:2000], float(cost_usd or 0)),
        )
        conn.execute(
            "DELETE FROM lane_logs WHERE lane_slug = ? AND id NOT IN "
            "(SELECT id FROM lane_logs WHERE lane_slug = ? ORDER BY id DESC LIMIT ?)",
            (slug, slug, LANE_LOG_CAP),
        )


def query_lane_logs(slug: str, limit: int = 200) -> list[dict]:
    """Most-recent-first log lines for a lane."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT ts, level, message, cost_usd FROM lane_logs WHERE lane_slug = ? "
            "ORDER BY id DESC LIMIT ?",
            (slug, max(1, min(limit, LANE_LOG_CAP))),
        ).fetchall()
    return [dict(r) for r in rows]


def spend_windows(slug: str | None, now: int) -> dict:
    """Rolling cost + request count over the last 5h and 7d. `slug=None` sums
    across ALL lanes (the account-wide view — the 5h/weekly limit is shared by
    every bot that runs on the same claude login). Only rows carrying a cost are
    counted as requests, so pure 'mention'/'reset' lines don't inflate the count."""
    h5, week = now - 5 * 3600, now - 7 * 86400
    where = "cost_usd > 0" + ("" if slug is None else " AND lane_slug = ?")
    args_h5 = ([h5] if slug is None else [h5, slug])
    args_wk = ([week] if slug is None else [week, slug])
    with connect() as conn:
        def agg(since_args, since_sql):
            r = conn.execute(
                f"SELECT COALESCE(SUM(cost_usd),0) AS usd, COUNT(*) AS n "
                f"FROM lane_logs WHERE ts >= ? AND {where}", since_args,
            ).fetchone()
            return {"usd": round(r["usd"], 4), "requests": r["n"]}
        return {"h5": agg(args_h5, h5), "week": agg(args_wk, week)}


# --- operator inbox (operator -> bot session, private two-way channel) ----


def add_operator_msg(lane_slug: str, text: str, from_user: str, ts: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO operator_inbox(lane_slug, text, from_user, ts) VALUES(?, ?, ?, ?)",
            (lane_slug, text[:4000], from_user or "operator", ts),
        )


def next_operator_msg(lane_slug: str) -> dict | None:
    """Oldest unhandled operator message for a lane."""
    with connect() as conn:
        r = conn.execute(
            "SELECT id, text, from_user, ts FROM operator_inbox "
            "WHERE lane_slug = ? AND status = 'pending' ORDER BY id LIMIT 1",
            (lane_slug,),
        ).fetchone()
    return dict(r) if r else None


def mark_operator_done(lane_slug: str, msg_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE operator_inbox SET status = 'done' WHERE lane_slug = ? AND id = ?",
            (lane_slug, msg_id),
        )


# --- messages ------------------------------------------------------------


def store_message(
    lane_slug: str,
    update_id: int,
    message_id: int | None,
    chat_id: int | None,
    chat_title: str | None,
    from_user: str,
    text: str,
    date: int,
    is_outgoing: bool = False,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages "
            "(lane_slug, update_id, message_id, chat_id, chat_title, from_user, text, date, is_outgoing) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lane_slug, update_id, message_id, chat_id, chat_title, from_user, text, date, int(is_outgoing)),
        )
        if chat_id is not None:
            conn.execute(
                "INSERT INTO seen_chats(lane_slug, chat_id, title, last_date) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(lane_slug, chat_id) DO UPDATE SET "
                "title = excluded.title, last_date = MAX(last_date, excluded.last_date)",
                (lane_slug, chat_id, chat_title or "", date),
            )


def next_outgoing_update_id(lane_slug: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(update_id), ?) AS m FROM messages WHERE lane_slug = ? AND update_id >= ?",
            (OUTGOING_BASE - 1, lane_slug, OUTGOING_BASE),
        ).fetchone()
        return row["m"] + 1


def max_incoming_update_id(lane_slug: str) -> int:
    """Highest update_id among incoming (real Telegram) messages, ignoring the
    high-namespace synthetic ids of the lane's own sends. 0 if none yet.

    Used to seed the wake cursor 'from now' so enabling wake never replays
    historical @mentions."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(update_id), 0) AS m FROM messages "
            "WHERE lane_slug = ? AND is_outgoing = 0 AND update_id < ?",
            (lane_slug, OUTGOING_BASE),
        ).fetchone()
        return row["m"]


def _msg_to_dict(r: sqlite3.Row) -> dict:
    return {
        "lane": r["lane_slug"],
        "updateId": r["update_id"],
        "messageId": r["message_id"],
        "chatId": r["chat_id"],
        "chatTitle": r["chat_title"],
        "from": r["from_user"],
        "text": r["text"],
        "date": r["date"],
        "outgoing": bool(r["is_outgoing"]),
    }


def query_messages(lane_slug: str, since: int, limit: int, order: str) -> list[dict]:
    direction = "DESC" if order == "desc" else "ASC"
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE lane_slug = ? AND update_id > ? "
            f"ORDER BY update_id {direction} LIMIT ?",
            (lane_slug, since, limit),
        ).fetchall()
        return [_msg_to_dict(r) for r in rows]


def query_feed(since_date: int, limit: int, order: str, chat_id: int | None = None) -> list[dict]:
    """Whole-chat merged feed across every lane.

    Human messages are captured by every bot in the chat (each under its own
    update_id), so rows are deduped by (chat_id, message_id). Each bot's own
    outgoing rows exist only in its lane and survive the merge. Sorted by date
    (update_ids are per-bot and not comparable across lanes).
    """
    where = "date > ?"
    params: list = [since_date]
    if chat_id is not None:
        where += " AND chat_id = ?"
        params.append(chat_id)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY date, message_id", params
        ).fetchall()
    picked: dict[tuple, sqlite3.Row] = {}
    for r in rows:
        key = (r["chat_id"], r["message_id"]) if r["message_id"] is not None else (
            r["lane_slug"], r["update_id"], None
        )
        if key not in picked:
            picked[key] = r
    merged = list(picked.values())
    if order == "desc":
        merged.reverse()
    return [_msg_to_dict(r) for r in merged[:limit]]


def count_messages(lane_slug: str | None = None) -> int:
    with connect() as conn:
        if lane_slug:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE lane_slug = ?", (lane_slug,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()
        return row["c"]


def seen_chats(lane_slug: str | None = None) -> list[dict]:
    with connect() as conn:
        if lane_slug:
            rows = conn.execute(
                "SELECT chat_id, title, MAX(last_date) AS last_date FROM seen_chats "
                "WHERE lane_slug = ? GROUP BY chat_id ORDER BY last_date DESC",
                (lane_slug,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT chat_id, title, MAX(last_date) AS last_date FROM seen_chats "
                "GROUP BY chat_id ORDER BY last_date DESC"
            ).fetchall()
        return [
            {"chatId": r["chat_id"], "title": r["title"], "lastDate": r["last_date"]} for r in rows
        ]
