#!/usr/bin/env bash
# Send a short status report to the team Telegram chat — via LaneHub.
#
# Since 2026-08-04 the devbot is a LaneHub lane (lanehub.kiras.life): the hub
# owns the bot's webhook and records everything it sends, so other lanes'
# agents see our messages in their /feed. We keep appending to the local
# tg-chat-log.jsonl too — it remains the durable, versioned history.
#
# NOTE: the hub sends PLAIN TEXT (no parse_mode) — Markdown like *bold* will
# show up literally. Write reports as plain text.
#
# Config lives in ./.lanehub.env (local only, NOT in any git repo):
#   LANEHUB_BASE=https://lanehub.kiras.life
#   LANEHUB_LANE=homeflow_assistant_devbot
#   LANEHUB_API_KEY=...
# Optional override: TG_CHAT_ID env var targets another chat for one send.
#
# Usage:
#   ./tg-report.sh "Done X. Waiting on Y."
#   echo "multi-line report" | ./tg-report.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HERE/.lanehub.env"
if [ -f "$CONF" ]; then
  # shellcheck disable=SC1090
  . "$CONF"
fi
: "${LANEHUB_BASE:?Missing LANEHUB_BASE (set it in .lanehub.env)}"
: "${LANEHUB_LANE:?Missing LANEHUB_LANE (set it in .lanehub.env)}"
: "${LANEHUB_API_KEY:?Missing LANEHUB_API_KEY (set it in .lanehub.env)}"

# Message from arg or stdin.
if [ "$#" -ge 1 ]; then
  MSG="$1"
else
  MSG="$(cat)"
fi

# Optional per-send chat override (defaults to the lane's default chat).
CHAT_OVERRIDE="${TG_CHAT_ID:-}"
BODY="$(jq -cn --arg text "$MSG" --arg chat "$CHAT_OVERRIDE" \
  'if $chat == "" then {text:$text} else {text:$text, chatId:$chat} end')"

resp="$(curl -s -X POST "${LANEHUB_BASE}/${LANEHUB_LANE}/send" \
  -H "X-Bridge-Token: ${LANEHUB_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$BODY")"

# --- durable history -------------------------------------------------------
# The hub stores every send in its own DB, but tg-chat-log.jsonl stays our
# versioned source of truth (same format as before the LaneHub migration).
LOG="$HERE/tg-chat-log.jsonl"
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ok="$(printf '%s' "$resp" | jq -r '.ok // false')"
mid="$(printf '%s' "$resp" | jq -r '.messageId // "null"')"
chat="$(printf '%s' "$resp" | jq -r '.chatId // "null"')"
jq -cn --arg ts "$ts" --arg chat "$chat" --argjson mid "$mid" \
  --argjson ok "$ok" --arg text "$MSG" \
  '{ts:$ts,dir:"out",via:"lanehub",chat_id:$chat,message_id:$mid,ok:$ok,text:$text}' >> "$LOG"

if [ "$ok" = "true" ]; then
  echo "sent ✓ via lanehub (logged → tg-chat-log.jsonl)"
else
  echo "FAILED:"
  printf '%s\n' "$resp"
  exit 1
fi
