#!/usr/bin/env bash
# Download a photo / document someone posted in the team chat.
#
# Attachments show up in tg-chat-log.jsonl (and the hub /feed) as rows with a
# `media` object: {kind, fileId, name, mime, size}. Telegram file_ids are only
# valid for the bot that received them, so the hub proxies the download with
# this lane's bot token (Bot API limit: 20 MB per file).
#
#   ./tg-file.sh <fileId>              → ./tg-files/<name>   (name from the log row, else <fileId>.<ext>)
#   ./tg-file.sh <fileId> out.jpg      → out.jpg
#   ./tg-file.sh --last                → newest attachment in tg-chat-log.jsonl
#
# Prints the saved path on stdout — then open it with your file reader.
#
# Config: ./.lanehub.env . Needs curl + jq.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/.lanehub.env" ] && . "$HERE/.lanehub.env"
: "${LANEHUB_BASE:?Missing LANEHUB_BASE (.lanehub.env)}"
: "${LANEHUB_LANE:?Missing LANEHUB_LANE (.lanehub.env)}"
: "${LANEHUB_API_KEY:?Missing LANEHUB_API_KEY (.lanehub.env)}"

LOG="$HERE/tg-chat-log.jsonl"
fid="${1:-}"
out="${2:-}"
[ -n "$fid" ] || { echo "usage: ./tg-file.sh <fileId> [out-path] | ./tg-file.sh --last"; exit 1; }

if [ "$fid" = "--last" ]; then
  fid="$(jq -r 'select(.media != null and .media.fileId != null) | .media.fileId' "$LOG" 2>/dev/null | tail -n 1)"
  [ -n "$fid" ] || { echo "no attachments in tg-chat-log.jsonl (run ./tg-fetch.sh first)"; exit 1; }
fi

if [ -z "$out" ]; then
  name="$(jq -r --arg f "$fid" 'select(.media != null and .media.fileId == $f) | .media.name // empty' "$LOG" 2>/dev/null | tail -n 1)"
  mkdir -p "$HERE/tg-files"
  out="$HERE/tg-files/${name:-$fid}"
fi

hdrs="$(mktemp)"
code="$(curl -sS -o "$out" -D "$hdrs" -w '%{http_code}' \
  "${LANEHUB_BASE}/${LANEHUB_LANE}/file/${fid}" -H "X-Bridge-Token: ${LANEHUB_API_KEY}")"

if [ "$code" != "200" ]; then
  echo "FAILED (HTTP $code): $(cat "$out" 2>/dev/null)"; rm -f "$out" "$hdrs"; exit 1
fi

# No name known and no extension → take it from the content-type.
case "$out" in
  *.*) ;;
  *)
    ct="$(grep -i '^content-type:' "$hdrs" | tail -n 1 | tr -d '\r' | awk '{print tolower($2)}')"
    case "$ct" in
      image/jpeg*) mv "$out" "$out.jpg"; out="$out.jpg" ;;
      image/png*)  mv "$out" "$out.png"; out="$out.png" ;;
      image/webp*) mv "$out" "$out.webp"; out="$out.webp" ;;
      application/pdf*) mv "$out" "$out.pdf"; out="$out.pdf" ;;
    esac ;;
esac
rm -f "$hdrs"
echo "$out"
