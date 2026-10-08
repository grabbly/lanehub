"""Import message rows from another LaneHub SQLite database into this hub.

Usage:
    python scripts/import_messages.py OLD_DB [--since UNIX_SECONDS] [--lane OLD_SLUG[=NEW_SLUG] ...] [--dry-run]

The script opens OLD_DB read-only and imports messages newer than --since (default 0).
New messages are inserted via db.store_message(), which assigns a new monotonic hub-wide
`seq` cursor unless a copy of the same (chat_id, message_id) already exists. This ensures
that imported rows show up to downstream agents following the feed cursor.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Ensure the repository root is on sys.path so 'app' can be imported.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import db
from app.config import settings


class _ArgumentParser(argparse.ArgumentParser):
    """Custom ArgumentParser that exits with status code 2 on argument errors."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"Error: {message}", file=sys.stderr)
        sys.exit(2)


def build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(
        description="Copy message rows from another LaneHub SQLite database into this hub.",
        prog="import_messages.py",
    )
    parser.add_argument(
        "old_db",
        metavar="OLD_DB",
        help="Path to the source LaneHub SQLite database file.",
    )
    parser.add_argument(
        "--since",
        type=int,
        default=0,
        metavar="UNIX_SECONDS",
        help="Import messages with date > UNIX_SECONDS (default: 0).",
    )
    parser.add_argument(
        "--lane",
        action="extend",
        nargs="+",
        default=None,
        metavar="OLD_SLUG[=NEW_SLUG]",
        help="Map old lane slugs to new slugs: OLD=NEW or OLD alone. Repeatable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate import without inserting rows into the target database.",
    )
    return parser


def parse_lane_mapping(lane_args: list[str] | None) -> tuple[dict[str, str] | None, str | None]:
    """Parse --lane arguments into a mapping from old slug to new slug.

    Returns (mapping, None) on success or (None, error_message) on failure.
    """
    if lane_args is None:
        return None, None
    mapping: dict[str, str] = {}
    for item in lane_args:
        if "=" in item:
            parts = item.split("=", 1)
            old_s, new_s = parts[0].strip(), parts[1].strip()
        else:
            old_s = new_s = item.strip()
        if not old_s or not new_s:
            return None, f"Invalid --lane specification '{item}': expected OLD_SLUG or OLD_SLUG=NEW_SLUG"
        mapping[old_s] = new_s
    return mapping, None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    lane_map, err = parse_lane_mapping(args.lane)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 2

    old_db_path = Path(args.old_db)
    if not old_db_path.exists():
        print(f"Error: database file not found: {args.old_db}", file=sys.stderr)
        return 2
    if not old_db_path.is_file():
        print(f"Error: path is not a file: {args.old_db}", file=sys.stderr)
        return 2

    # Open OLD_DB read-only
    try:
        old_conn = sqlite3.connect(f"file:{old_db_path.resolve()}?mode=ro", uri=True)
        old_conn.row_factory = sqlite3.Row
    except (sqlite3.Error, OSError) as exc:
        print(f"Error opening source database {args.old_db}: {exc}", file=sys.stderr)
        return 2

    try:
        table_info = old_conn.execute("PRAGMA table_info(messages)").fetchall()
    except (sqlite3.Error, OSError) as exc:
        print(f"Error reading source database {args.old_db}: {exc}", file=sys.stderr)
        old_conn.close()
        return 2

    if not table_info:
        print(f"Error: 'messages' table not found in source database {args.old_db}", file=sys.stderr)
        old_conn.close()
        return 2

    existing_cols = {r["name"] for r in table_info}
    required_cols = {
        "lane_slug",
        "update_id",
        "message_id",
        "chat_id",
        "chat_title",
        "from_user",
        "text",
        "date",
        "is_outgoing",
    }
    missing_required = required_cols - existing_cols
    if missing_required:
        print(
            f"Error: source 'messages' table in {args.old_db} is missing required columns: {sorted(missing_required)}",
            file=sys.stderr,
        )
        old_conn.close()
        return 2

    # Ensure target DB schema and migrations exist
    try:
        db.connect().close()
    except Exception as exc:
        print(f"Error initializing target database {settings.db_path}: {exc}", file=sys.stderr)
        old_conn.close()
        return 2

    # Prepare SELECT query for OLD_DB
    select_cols = [
        "lane_slug",
        "update_id",
        "message_id",
        "chat_id",
        "chat_title",
        "from_user",
        "text",
        "date",
        "is_outgoing",
        "media" if "media" in existing_cols else "NULL AS media",
        "from_id" if "from_id" in existing_cols else "NULL AS from_id",
        "from_username" if "from_username" in existing_cols else "NULL AS from_username",
        "from_is_bot" if "from_is_bot" in existing_cols else "NULL AS from_is_bot",
    ]

    query = f"SELECT {', '.join(select_cols)} FROM messages WHERE date > ?"
    params: list[Any] = [args.since]
    if lane_map is not None:
        placeholders = ", ".join("?" for _ in lane_map)
        query += f" AND lane_slug IN ({placeholders})"
        params.extend(lane_map.keys())
    query += " ORDER BY date, update_id"

    try:
        rows = old_conn.execute(query, params).fetchall()
    except (sqlite3.Error, OSError) as exc:
        print(f"Error querying messages from {args.old_db}: {exc}", file=sys.stderr)
        old_conn.close()
        return 2

    target_conn = db.connect()
    try:
        target_lane_cache: dict[str, bool] = {}

        def target_lane_exists(slug: str) -> bool:
            if slug not in target_lane_cache:
                target_lane_cache[slug] = db.get_lane(slug) is not None
            return target_lane_cache[slug]

        no_such_lane = 0
        lane_stats: dict[str, dict[str, int]] = {}

        # If explicit lanes were provided, pre-populate stats for target lanes that exist
        if lane_map is not None:
            for tgt in sorted(set(lane_map.values())):
                if target_lane_exists(tgt):
                    lane_stats[tgt] = {"inserted": 0, "already_there": 0}

        seen_in_run: set[tuple[str, int]] = set()

        for r in rows:
            old_slug = r["lane_slug"]
            target_slug = lane_map[old_slug] if lane_map is not None else old_slug

            if not target_lane_exists(target_slug):
                no_such_lane += 1
                continue

            lane_stats.setdefault(target_slug, {"inserted": 0, "already_there": 0})
            update_id = int(r["update_id"])
            row_key = (target_slug, update_id)

            if row_key in seen_in_run:
                lane_stats[target_slug]["already_there"] += 1
                continue

            # Dedup check in target DB
            exists = target_conn.execute(
                "SELECT 1 FROM messages WHERE lane_slug = ? AND update_id = ?",
                row_key,
            ).fetchone()

            if exists:
                lane_stats[target_slug]["already_there"] += 1
                continue

            seen_in_run.add(row_key)
            if args.dry_run:
                lane_stats[target_slug]["inserted"] += 1
            else:
                raw_media = r["media"]
                media_dict = None
                if raw_media:
                    try:
                        media_dict = json.loads(raw_media) if isinstance(raw_media, str) else raw_media
                    except (json.JSONDecodeError, TypeError):
                        media_dict = None

                from_is_bot = None if r["from_is_bot"] is None else bool(r["from_is_bot"])

                db.store_message(
                    lane_slug=target_slug,
                    update_id=update_id,
                    message_id=int(r["message_id"]) if r["message_id"] is not None else None,
                    chat_id=int(r["chat_id"]) if r["chat_id"] is not None else None,
                    chat_title=r["chat_title"],
                    from_user=r["from_user"] or "",
                    text=r["text"] or "",
                    date=int(r["date"]) if r["date"] is not None else 0,
                    is_outgoing=bool(r["is_outgoing"]),
                    media=media_dict,
                    from_id=int(r["from_id"]) if r["from_id"] is not None else None,
                    from_username=r["from_username"],
                    from_is_bot=from_is_bot,
                )
                lane_stats[target_slug]["inserted"] += 1
    finally:
        target_conn.close()
        old_conn.close()

    # Report
    if args.dry_run:
        print("[dry-run] No changes written to database.")
    for lane, s in sorted(lane_stats.items()):
        print(f"{lane}: {s['inserted']} inserted, {s['already_there']} already there")
    print(f"Total 'no such lane' skipped: {no_such_lane}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
