#!/usr/bin/env bash
# Ask the operator a clarifying question — posts it to THIS bot's operator chat
# via LaneHub. The operator's reply comes back into your session automatically,
# so after asking, stop and wait; you'll be resumed with their answer.
#
#   ./ask-operator.sh "uk or pl for this user?"
#   echo "long question" | ./ask-operator.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/.lanehub.env" ] && . "$HERE/.lanehub.env"
: "${LANEHUB_BASE:?Missing LANEHUB_BASE (.lanehub.env)}"
: "${LANEHUB_LANE:?Missing LANEHUB_LANE (.lanehub.env)}"
: "${LANEHUB_API_KEY:?Missing LANEHUB_API_KEY (.lanehub.env)}"

MSG="${1:-$(cat)}"
[ -n "$MSG" ] || { echo "usage: ./ask-operator.sh \"question\""; exit 1; }

resp="$(curl -sS -X POST "${LANEHUB_BASE}/${LANEHUB_LANE}/ask" \
  -H "X-Bridge-Token: ${LANEHUB_API_KEY}" -H "Content-Type: application/json" \
  -d "$(jq -cn --arg t "$MSG" '{text:$t}')")"

if printf '%s' "$resp" | grep -q '"ok":true'; then
  echo "asked the operator ✓ — now stop and wait; you'll be resumed with their reply"
else
  echo "FAILED to reach operator (is an operator chat set for this lane?): $resp"
  exit 1
fi
