#!/usr/bin/env bash
# stop_bot.sh — gracefully stop all bot services
# Usage: ./scripts/stop_bot.sh

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/logs"

_stop() {
  local name="$1"
  local pidfile="$LOG_DIR/$name.pid"
  if [ -f "$pidfile" ]; then
    local pid
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      echo "  Stopped $name (PID $pid)"
    else
      echo "  $name (PID $pid) already stopped"
    fi
    rm -f "$pidfile"
  else
    echo "  No $name.pid found"
  fi
}

echo "Stopping services..."
_stop watchdog
_stop bot
_stop collector
_stop dashboard
_stop mlb_scanner
_stop stats_exporter
_stop stats_server
echo "Done."
