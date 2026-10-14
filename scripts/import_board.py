#!/usr/bin/env python3
"""Import board tasks and ledger entries from prototype artifact exports or JSON files."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.boards import store
from app.config import settings


def parse_ledger_arg(arg: str) -> tuple[str, float, str, str]:
    parts = arg.split(":", 3)
    if len(parts) < 3:
        raise ValueError(f"Invalid ledger format '{arg}'. Expected kind:amount:at[:note]")
    kind = parts[0].strip().lower()
    amount = float(parts[1].strip().replace(",", "."))
    at_date = parts[2].strip()
    note = parts[3].strip() if len(parts) > 3 else ""
    return (kind, amount, at_date, note)


def load_tasks_from_path(export_path: Path) -> list[dict]:
    if not export_path.exists():
        raise FileNotFoundError(f"Export path '{export_path}' does not exist")

    tasks_data = []
    if export_path.is_dir():
        for file in sorted(export_path.glob("*.json")):
            if file.name.startswith("."):
                continue  # macOS AppleDouble (._x.json) and other hidden files
            with open(file, "r", encoding="utf-8") as f:
                content = json.load(f)
                if isinstance(content, list):
                    tasks_data.extend(content)
                else:
                    # An artifact export keeps the document id only in the
                    # file name (<doc_id>.json); carry it so ids survive.
                    if isinstance(content, dict) and "id" not in content:
                        content = {"id": file.stem, **content}
                    tasks_data.append(content)
    else:
        with open(export_path, "r", encoding="utf-8") as f:
            content = json.load(f)
            if isinstance(content, list):
                tasks_data = content
            elif isinstance(content, dict):
                if "tasks" in content:
                    tasks_data = content["tasks"]
                elif "cards" in content:
                    tasks_data = content["cards"]
                else:
                    tasks_data = [content]
            else:
                raise ValueError("Unexpected JSON content")

    return tasks_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Import board tasks into LaneHub.")
    parser.add_argument("--db", type=str, help="Path to SQLite database", default="")
    parser.add_argument("--board", type=str, required=True, help="Board slug")
    parser.add_argument("--create", action="store_true", help="Create board if it does not exist")
    parser.add_argument("--chat-id", type=str, default="", help="Telegram chat ID for board")
    parser.add_argument("--title", type=str, default="", help="Board title")
    parser.add_argument("--team-lang", type=str, default="ru", help="Team language (default: ru)")
    parser.add_argument("--client-lang", type=str, default="uk", help="Client language (default: uk)")
    parser.add_argument("--rate", type=float, default=40.0, help="Hourly rate (default: 40)")
    parser.add_argument("--currency", type=str, default="zł", help="Currency symbol (default: zł)")
    parser.add_argument(
        "--ledger",
        action="append",
        default=[],
        help="Ledger entry 'kind:amount:at[:note]', can be repeated",
    )
    parser.add_argument("--client-token-stdin", action="store_true", help="Read client token from stdin")
    parser.add_argument("--dry-run", action="store_true", help="Run without persisting changes")
    parser.add_argument("export_path", type=str, help="Path to exported directory or JSON file")

    args = parser.parse_args()

    if args.db:
        settings.db_path = Path(args.db)

    client_token = None
    if args.client_token_stdin:
        client_token = sys.stdin.read().strip()
        if not client_token:
            print("Warning: --client-token-stdin specified but empty token received", file=sys.stderr)
            client_token = None

    ledger_entries = [parse_ledger_arg(item) for item in args.ledger]
    export_path = Path(args.export_path)
    tasks_data = load_tasks_from_path(export_path)

    create_params = None
    if args.create:
        create_params = {
            "chat_id": args.chat_id,
            "title": args.title,
            "team_lang": args.team_lang,
            "client_lang": args.client_lang,
            "currency": args.currency,
            "rate": args.rate,
        }

    res = store.import_board(
        board_slug=args.board,
        tasks_data=tasks_data,
        ledger_entries=ledger_entries,
        dry_run=args.dry_run,
        create_params=create_params,
        client_token=client_token,
        actor="importer",
    )

    print(f"Import summary for board '{res['boardSlug']}':")
    print(f"  Created tasks: {res['createdTasks']}")
    print(f"  Updated tasks: {res['updatedTasks']}")
    print(f"  Created ledger entries: {res['createdLedger']}")
    print(f"  Dry run: {res['dryRun']}")
    if res["warnings"]:
        print(f"  Warnings ({len(res['warnings'])}):")
        for w in res["warnings"]:
            print(f"    - {w}")


if __name__ == "__main__":
    main()
