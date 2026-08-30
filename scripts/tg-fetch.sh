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
# Incremental state: .lanehub.feed.since holds the last seen unix `date`.
# The feed filter is strictly date > since, so we re-fetch the boundary
# second and dedupe by (lane, update_id) against the tail of the local log.
# Our own lane's outgoing rows are skipped (tg-report.sh already logs them).
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
since="$(cat "$SINCE_FILE" 2>/dev/null || echo 0)"
# Re-fetch the boundary second (feed uses date > since); dedupe handles overlap.
[ "$since" -gt 0 ] && since=$((since - 1))

resp="$(curl -s "${LANEHUB_BASE}/${LANEHUB_LANE}/feed?order=asc&limit=500&sinceDate=${since}" \
  -H "X-Bridge-Token: ${LANEHUB_API_KEY}")"

if ! printf '%s' "$resp" | jq -e '.messages' > /dev/null 2>&1; then
  echo "FAILED:"
  printf '%s\n' "$resp"
  exit 1
fi

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
      chat_id: .chatId,
      from: .from,
      message_id: .messageId,
      date: .date,
      text: .text
    }')

# Advance the incremental cursor to the newest date seen.
last="$(printf '%s' "$resp" | jq -r '[.messages[].date] | max // empty')"
if [ -n "$last" ]; then
  echo "$last" > "$SINCE_FILE"
fi

echo "fetched $(printf '%s' "$resp" | jq '.messages | length') feed row(s), appended ${added} new → tg-chat-log.jsonl"
