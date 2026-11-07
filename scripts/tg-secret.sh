#!/usr/bin/env bash
# Hand a key / password / token to another lane's agent WITHOUT posting it in
# the chat. The hub keeps the value until the target lane reads it once (or it
# expires, 24 h by default); the chat only ever sees a ref like
# `lanehub-secret:Ab3…`, which is useless to anyone but that lane.
#
#   Send (the value comes from stdin, never from argv — argv leaks into ps and
#   shell history):
#     ./tg-secret.sh send <to-lane> [ttl-hours] [note] < key.txt
#     printf %s "$KEY" | ./tg-secret.sh send gabbsro-9 24 "ITHS test key"
#   It prints the ref; post it so the other agent knows:
#     ./tg-report.sh "@their_bot ITHS test key: $(./tg-secret.sh send gabbsro-9 < key.txt)"
#
#   Receive (one read, then the hub deletes it):
#     ./tg-secret.sh get <ref> <out-file>   → value saved to out-file (chmod 600)
#     ./tg-secret.sh get <ref> -            → value printed to stdout
#   Prefer the out-file form: an agent can then use the file without the value
#   ever landing in its transcript or logs.
#
# Config: ./.lanehub.env . Needs curl + jq.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/.lanehub.env" ] && . "$HERE/.lanehub.env"
: "${LANEHUB_BASE:?Missing LANEHUB_BASE (.lanehub.env)}"
: "${LANEHUB_LANE:?Missing LANEHUB_LANE (.lanehub.env)}"
: "${LANEHUB_API_KEY:?Missing LANEHUB_API_KEY (.lanehub.env)}"

usage() {
  echo "usage: ./tg-secret.sh send <to-lane> [ttl-hours] [note] < value-file"
  echo "       ./tg-secret.sh get <lanehub-secret:id> <out-file | ->"
  exit 1
}

cmd="${1:-}"
case "$cmd" in
  send)
    to="${2:-}"; [ -n "$to" ] || usage
    ttl="${3:-24}"
    note="${4:-}"
    if [ -t 0 ]; then
      read -r -s -p "Value for ${to} (hidden): " value; echo >&2
    else
      value="$(cat)"
    fi
    [ -n "$value" ] || { echo "empty value — nothing sent" >&2; exit 1; }
    resp="$(jq -cn --arg to "$to" --arg v "$value" --arg n "$note" --argjson t "$ttl" \
        '{to:$to, value:$v, note:$n, ttlHours:$t}' \
      | curl -sS -X POST "${LANEHUB_BASE}/${LANEHUB_LANE}/secret" \
          -H "X-Bridge-Token: ${LANEHUB_API_KEY}" -H "Content-Type: application/json" --data-binary @-)"
    ref="$(printf '%s' "$resp" | jq -r '.ref // empty' 2>/dev/null || true)"
    [ -n "$ref" ] || { echo "FAILED: $resp" >&2; exit 1; }
    echo "$ref"
    ;;
  get)
    ref="${2:-}"; out="${3:-}"
    [ -n "$ref" ] && [ -n "$out" ] || usage
    id="${ref#lanehub-secret:}"
    resp="$(curl -sS -w '\n%{http_code}' "${LANEHUB_BASE}/${LANEHUB_LANE}/secret/${id}" \
      -H "X-Bridge-Token: ${LANEHUB_API_KEY}")"
    code="${resp##*$'\n'}"; body="${resp%$'\n'*}"
    [ "$code" = "200" ] || { echo "FAILED (HTTP $code): $body" >&2; exit 1; }
    from="$(printf '%s' "$body" | jq -r '.from')"
    note="$(printf '%s' "$body" | jq -r '.note')"
    if [ "$out" = "-" ]; then
      printf '%s' "$body" | jq -j '.value'; echo
    else
      ( umask 077; printf '%s' "$body" | jq -j '.value' > "$out" )
      chmod 600 "$out"
      echo "saved → $out (from ${from}${note:+: $note}); the hub has deleted it"
    fi
    ;;
  *) usage ;;
esac
