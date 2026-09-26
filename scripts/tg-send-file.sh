#!/usr/bin/env bash
# Send a file (screenshot, report, log…) to the team Telegram chat — via
# LaneHub's POST /{lane}/sendFile. Images (jpeg/png/webp ≤10 MB) arrive as a
# photo, everything else as a document; 50 MB max. The hub posts only to the
# chat this lane is bound to.
#
# Config: ./.lanehub.env (same as tg-report.sh).
#
# Usage:
#   ./tg-send-file.sh report.pdf "Weekly report"
#   ./tg-send-file.sh --doc screenshot.png      # keep the image as a file (no recompression)
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

AS_DOC=false
if [ "${1:-}" = "--doc" ]; then
  AS_DOC=true
  shift
fi
FILE="${1:?usage: ./tg-send-file.sh [--doc] <file> [caption]}"
CAPTION="${2:-}"
[ -f "$FILE" ] || { echo "no such file: $FILE" >&2; exit 1; }

args=(-F "file=@${FILE}" -F "asDocument=${AS_DOC}")
[ -n "$CAPTION" ] && args+=(-F "caption=${CAPTION}")
[ -n "${LANEHUB_CHAT_ID:-}" ] && args+=(-F "chatId=${LANEHUB_CHAT_ID}")

resp="$(curl -s -X POST "${LANEHUB_BASE}/${LANEHUB_LANE}/sendFile" \
  -H "X-Bridge-Token: ${LANEHUB_API_KEY}" "${args[@]}")"

LOG="$HERE/tg-chat-log.jsonl"
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ok="$(printf '%s' "$resp" | jq -r '.ok // false')"
mid="$(printf '%s' "$resp" | jq -r '.messageId // "null"')"
chat="$(printf '%s' "$resp" | jq -r '.chatId // "null"')"
jq -cn --arg ts "$ts" --arg chat "$chat" --argjson mid "$mid" --argjson ok "$ok" \
  --arg text "$CAPTION" --arg file "$(basename "$FILE")" \
  '{ts:$ts,dir:"out",via:"lanehub",chat_id:$chat,message_id:$mid,ok:$ok,text:$text,file:$file}' >> "$LOG"

if [ "$ok" = "true" ]; then
  echo "sent ✓ $(basename "$FILE") via lanehub (logged → tg-chat-log.jsonl)"
else
  echo "FAILED:"
  printf '%s\n' "$resp"
  exit 1
fi
