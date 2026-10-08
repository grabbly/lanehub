#!/usr/bin/env bash
# Control autopilot mode for this lane — via LaneHub.
#
# Usage:
#   ./tg-autopilot.sh on [hours]   # start autopilot (default 8 h, max 72 h)
#   ./tg-autopilot.sh off          # switch autopilot off
#   ./tg-autopilot.sh status       # check hub autopilot status
#
# On macOS, 'on' talks to the menu-bar app LaneHub Autopilot via its URL scheme
# so running autopilot is always visible in the menu bar. If the app is not
# installed, it prints how to get it and exits 1.
# On non-macOS, 'on' switches autopilot on via the hub API and runs the watcher
# in the foreground.
# 'off' calls POST /{lane}/autopilot {on:false} to switch it off.
# 'status' calls GET /{lane}/autopilot and prints the state.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$HERE"
[ -f "$PROJECT_DIR/.lanehub.env" ] || PROJECT_DIR="$(pwd)"
CONF="$PROJECT_DIR/.lanehub.env"
if [ -f "$CONF" ]; then
  # shellcheck disable=SC1090
  . "$CONF"
fi
urlencode() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

: "${LANEHUB_BASE:?Missing LANEHUB_BASE (set it in .lanehub.env)}"
: "${LANEHUB_LANE:?Missing LANEHUB_LANE (set it in .lanehub.env)}"
: "${LANEHUB_API_KEY:?Missing LANEHUB_API_KEY (set it in .lanehub.env)}"

LANE_URL="${LANEHUB_BASE%/}/${LANEHUB_LANE}"
CMD="${1:-status}"

app_is_installed() {
  [ -d "/Applications/LaneHub Autopilot.app" ] || \
  [ -d "$HOME/Applications/LaneHub Autopilot.app" ] || \
  osascript -e 'id of app "LaneHub Autopilot"' >/dev/null 2>&1
}

case "$CMD" in
  on)
    HOURS="${2:-8}"
    if [ "$(uname)" = "Darwin" ]; then
      if app_is_installed; then
        open "lanehub-autopilot://start?dir=$(urlencode "$PROJECT_DIR")&hours=${HOURS}"
        echo "Autopilot start requested in LaneHub Autopilot menu-bar app (${HOURS}h)."
      else
        echo "LaneHub Autopilot menu-bar app is not installed."
        echo "Download it from: https://github.com/grabbly/lanehub/releases/latest/download/LaneHubAutopilot.zip"
        echo "Move 'LaneHub Autopilot.app' to /Applications and run it."
        exit 1
      fi
    else
      resp="$(curl -sS -X POST "${LANE_URL}/autopilot" \
        -H "X-Bridge-Token: ${LANEHUB_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "{\"on\": true, \"hours\": ${HOURS}}")"
      echo "$resp"
      WATCHER=""
      if [ -f "$PROJECT_DIR/watcher.py" ]; then
        WATCHER="$PROJECT_DIR/watcher.py"
      elif [ -f "$HERE/watcher.py" ]; then
        WATCHER="$HERE/watcher.py"
      elif [ -f "$HERE/telegram_watch.py" ]; then
        WATCHER="$HERE/telegram_watch.py"
      fi
      if [ -n "$WATCHER" ]; then
        echo "Starting watcher in foreground (Ctrl+C to stop)..."
        CLAUDE_PROJECT_DIR="$PROJECT_DIR" exec python3 "$WATCHER"
      else
        echo "Watcher script not found in current directory. Run watcher to start polling."
      fi
    fi
    ;;
  off)
    resp="$(curl -sS -X POST "${LANE_URL}/autopilot" \
      -H "X-Bridge-Token: ${LANEHUB_API_KEY}" \
      -H "Content-Type: application/json" \
      -d '{"on": false}')"
    echo "$resp"
    if [ "$(uname)" = "Darwin" ]; then
      open "lanehub-autopilot://stop?dir=$(urlencode "$PROJECT_DIR")" >/dev/null 2>&1 || true
    fi
    ;;
  status)
    resp="$(curl -sS "${LANE_URL}/autopilot" \
      -H "X-Bridge-Token: ${LANEHUB_API_KEY}")"
    echo "$resp"
    ;;
  *)
    echo "Usage: $0 {on [hours]|off|status}"
    exit 1
    ;;
esac
