#!/usr/bin/env bash
# watchdog.sh — restart trading bot if it crashes + auto-pull from GitHub
# Checks bot every 30s; syncs git every 5 min (every 10 ticks).

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/logs"
PIDFILE="$LOG_DIR/bot.pid"
SYNC_LOG="$LOG_DIR/git_sync.log"
MAX_RESTARTS=10
restart_count=0
tick=0

PYTHON="${PYTHON:-$REPO/venv/bin/python}"
if [ ! -f "$PYTHON" ]; then
  PYTHON="$(which python3)"
fi

echo "[watchdog] started — monitoring bot every 30s, git sync every 5 min"

_git_sync() {
  cd "$REPO" || return
  git fetch origin >> "$SYNC_LOG" 2>&1
  LOCAL=$(git rev-parse HEAD)
  REMOTE=$(git rev-parse origin/master)
  if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') New commits — pulling..." >> "$SYNC_LOG"
    git reset --hard origin/master >> "$SYNC_LOG" 2>&1
    echo "$(date '+%Y-%m-%d %H:%M:%S') Done: $(git log -1 --format='%h %s')" >> "$SYNC_LOG"
    touch "$REPO/dashboard/main_dashboard.py"
  fi
}

while true; do
  sleep 30
  tick=$((tick + 1))

  # Git sync every 10 ticks (5 min)
  if [ $((tick % 10)) -eq 0 ]; then
    _git_sync
  fi

  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE")"
    if kill -0 "$pid" 2>/dev/null; then
      continue
    fi
  fi

  # Bot is not running
  if [ "$restart_count" -ge "$MAX_RESTARTS" ]; then
    echo "[watchdog] $(date -u): max restarts ($MAX_RESTARTS) reached — giving up."
    exit 1
  fi

  restart_count=$((restart_count + 1))
  echo "[watchdog] $(date -u): bot not running — restart #$restart_count"

  nohup "$PYTHON" -m trading.bot_main \
    >> "$LOG_DIR/bot.log" 2>&1 &
  echo $! > "$PIDFILE"
  echo "[watchdog] $(date -u): bot restarted (PID $(cat "$PIDFILE"))"
done
