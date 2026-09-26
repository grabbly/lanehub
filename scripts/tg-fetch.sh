#!/usr/bin/env bash
# Fetch new team-chat messages from LaneHub's merged /feed and append them to
# tg-chat-log.jsonl — the companion to tg-report.sh's outgoing log.
#
# Since 2026-08-04 the devbot's updates go to LaneHub (webhook mode), so
# getUpdates is dead (409). The hub keeps the full history in its own DB;
# this script pulls increments and keeps our local, versioned log going.
# The /feed is merged across ALL lanes — once teammates' bots join the hub,
# their messages appear here too (plain getUpdates could never see them).
#
# Incremental state: .lanehub.feed.cursor holds the hub's `nextCursor` (the
# feed's monotonic `seq`); each run pages /feed?after=<cursor> until empty, so
# nothing is skipped or fetched twice. First run on an old install bootstraps
# from .lanehub.feed.since (last seen unix `date`, the pre-0.5 cursor), and a
# hub older than 0.5 (no nextCursor) keeps using the date cursor. Rows are
# still deduped by (lane, update_id) against the log tail as a safety net.
# Our own lane's outgoing rows are skipped (tg-report.sh already logs them).
# Rows with an attachment carry `media` ({kind, fileId, name, mime, size});
# download it with ./tg-file.sh <fileId>.
#
# Config: ./.lanehub.env . Usage: ./tg-fetch.sh
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

LOG="$HERE/tg-chat-log.jsonl"
SINCE_FILE="$HERE/.lanehub.feed.since"
CURSOR_FILE="$HERE/.lanehub.feed.cursor"

fetch() {
  local resp
  resp="$(curl -s "${LANEHUB_BASE}/${LANEHUB_LANE}/feed?order=asc&limit=500&$1" \
    -H "X-Bridge-Token: ${LANEHUB_API_KEY}")"
  if ! printf '%s' "$resp" | jq -e '.messages' > /dev/null 2>&1; then
    echo "FAILED:" >&2
    printf '%s\n' "$resp" >&2
    exit 1
  fi
  printf '%s' "$resp"
}

ROWS="$(mktemp)"
trap 'rm -f "$ROWS"' EXIT
cursor="$(cat "$CURSOR_FILE" 2>/dev/null || true)"
if [ -n "$cursor" ]; then
  while :; do
    page="$(fetch "after=${cursor}")"
    n="$(printf '%s' "$page" | jq '.messages | length')"
    [ "$n" -eq 0 ] && break
    printf '%s' "$page" | jq -c '.messages[]' >> "$ROWS"
    cursor="$(printf '%s' "$page" | jq -r '.nextCursor')"
    [ "$n" -lt 500 ] && break
  done
else
  since="$(cat "$SINCE_FILE" 2>/dev/null || echo 0)"
  # Re-fetch the boundary second (sinceDate is date > since); dedupe handles overlap.
  [ "$since" -gt 0 ] && since=$((since - 1))
  page="$(fetch "sinceDate=${since}")"
  printf '%s' "$page" | jq -c '.messages[]' >> "$ROWS"
  cursor="$(printf '%s' "$page" | jq -r '.nextCursor // empty')"
fi
resp="$(jq -c -s '{messages: .}' "$ROWS")"

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Keys already present in the log tail (lane:update_id), for dedupe.
seen="$(tail -n 400 "$LOG" 2>/dev/null | jq -r 'select(.lane != null) | "\(.lane):\(.update_id)"' 2>/dev/null | sort -u || true)"

added=0
while IFS= read -r row; do
  key="$(printf '%s' "$row" | jq -r '"\(.lane):\(.update_id)"')"
  if ! printf '%s\n' "$seen" | grep -qxF "$key"; then
    printf '%s\n' "$row" >> "$LOG"
    added=$((added + 1))
  fi
done < <(printf '%s' "$resp" | jq -c --arg ts "$ts" --arg own "$LANEHUB_LANE" '
  .messages[]
  # skip our own outgoing rows — tg-report.sh logs them at send time
  | select((.outgoing | not) or (.lane != $own))
  | {
      ts: $ts,
      dir: (if .outgoing then "out-lane" else "in" end),
      lane: .lane,
      update_id: .updateId,
      seq: .seq,
      chat_id: .chatId,
      from: .from,
      from_is_bot: .fromIsBot,
      message_id: .messageId,
      date: .date,
      text: .text,
      media: .media
    }')

# Advance the cursors: seq (0.5+ hubs) and the newest date (fallback).
if [ -n "$cursor" ] && [ "$cursor" != "null" ]; then
  echo "$cursor" > "$CURSOR_FILE"
fi
last="$(printf '%s' "$resp" | jq -r '[.messages[].date] | max // empty')"
if [ -n "$last" ]; then
  echo "$last" > "$SINCE_FILE"
fi

echo "fetched $(printf '%s' "$resp" | jq '.messages | length') feed row(s), appended ${added} new → tg-chat-log.jsonl"
