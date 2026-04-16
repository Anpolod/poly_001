#!/usr/bin/env bash
# watchdog.sh — supervise every long-running daemon launched by start_bot.sh,
# restart them on crash, restart them after git pull so deployed code actually
# runs. Checks every 30s; syncs git every 5 min (every 10 ticks).
#
# Round-4 fix: supervision now covers all 6 daemons (was just bot + collector),
# closing the version-skew gap codex flagged. Each process has its own
# restart counter + HEALTHY_RESET_TICKS reset so routine deploys don't burn
# the MAX_RESTARTS budget.

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/logs"
SYNC_LOG="$LOG_DIR/git_sync.log"
MAX_RESTARTS=10
HEALTHY_RESET_TICKS=10   # 10 ticks × 30s = 5 min of uptime earns the counter back
tick=0

PYTHON="${PYTHON:-$REPO/venv/bin/python}"
if [ ! -f "$PYTHON" ]; then
  PYTHON="$(which python3)"
fi

# Round-4 fix: source .env so LOCAL_BIND_IP / POLYGON_PRIVATE_KEY / etc. are
# inherited by every nohup'd child. Previously only trading/bot_main.py loaded
# .env itself, so the collector and sidecar daemons never saw those vars.
if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO/.env"
  set +a
fi

# ── Daemon table ────────────────────────────────────────────────────────────
# Parallel indexed arrays (bash 3.2 compatible — macOS default shell).
# Indices must line up across all four arrays.
# T-45: mlb_scanner is appended conditionally on the config flag below so
# `enabled: false` (or missing section) actually disables it instead of
# watchdog restarting it forever.
DAEMON_NAMES=(bot collector dashboard stats_exporter stats_server)
DAEMON_CMDS=(
  "$PYTHON -m trading.bot_main"
  "$PYTHON $REPO/main.py"
  "$PYTHON -m streamlit run $REPO/dashboard/main_dashboard.py --server.port 8501 --server.headless true --server.address 0.0.0.0"
  "$PYTHON $REPO/dashboard/export_dashboard_data.py --watch"
  "$PYTHON -m http.server 8502 --directory $REPO/dashboard"
)
RESTART_COUNTS=(0 0 0 0 0)
HEALTHY_TICKS=(0 0 0 0 0)

# T-45: append mlb_scanner only when config gates it in.
MLB_ENABLED=$("$PYTHON" "$REPO/scripts/config_get.py" mlb_pitcher_scanner.enabled 2>/dev/null || echo "")
if [ "$MLB_ENABLED" = "true" ]; then
  DAEMON_NAMES+=(mlb_scanner)
  DAEMON_CMDS+=("$PYTHON -m analytics.mlb_pitcher_scanner --watch --save")
  RESTART_COUNTS+=(0)
  HEALTHY_TICKS+=(0)
  echo "[watchdog] mlb_pitcher_scanner enabled — supervising"
else
  echo "[watchdog] mlb_pitcher_scanner disabled — not supervising"
fi

echo "[watchdog] started — supervising ${#DAEMON_NAMES[@]} daemons every 30s, git sync every 5 min"

_git_sync() {
  cd "$REPO" || return
  git fetch origin >> "$SYNC_LOG" 2>&1
  LOCAL=$(git rev-parse HEAD)
  REMOTE=$(git rev-parse origin/master)
  if [ "$LOCAL" = "$REMOTE" ]; then
    return
  fi

  echo "$(date '+%Y-%m-%d %H:%M:%S') New commits — pulling..." >> "$SYNC_LOG"
  # T-44: record PREV_HEAD BEFORE the reset so we can roll back if preflight
  # fails on the pulled code. Without this, a migration miss killed children
  # first and THEN discovered it couldn't run them — turning a recoverable
  # config drift into a deploy outage.
  PREV_HEAD="$LOCAL"
  git reset --hard origin/master >> "$SYNC_LOG" 2>&1
  echo "$(date '+%Y-%m-%d %H:%M:%S') Done: $(git log -1 --format='%h %s')" >> "$SYNC_LOG"

  # T-44: preflight BEFORE touching any daemon. Healthy processes running on
  # the previous commit keep serving until we know the new schema is usable.
  # Codex round 8 HIGH finding: watchdog previously killed children first and
  # then noticed preflight failure — an avoidable outage on migration miss.
  if [ -f "$REPO/scripts/db_preflight.py" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Running DB schema preflight after pull" >> "$SYNC_LOG"
    if ! "$PYTHON" "$REPO/scripts/db_preflight.py" >> "$SYNC_LOG" 2>&1; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') ⚠️  SCHEMA PREFLIGHT FAILED — rolling back to $PREV_HEAD; daemons keep running on old code" >> "$SYNC_LOG"
      git reset --hard "$PREV_HEAD" >> "$SYNC_LOG" 2>&1
      echo "$(date '+%Y-%m-%d %H:%M:%S') Rollback done. Operator: apply db/schema.sql, next tick will re-attempt pull." >> "$SYNC_LOG"
      # Leave children untouched. Return WITHOUT exec — next sync tick retries
      # the fetch/preflight cycle. Rollback puts us back on PREV_HEAD so the
      # next `git rev-parse HEAD != origin/master` check fires again.
      return
    fi
  fi

  # Preflight passed — safe to kill children and restart on new code.
  for name in "${DAEMON_NAMES[@]}"; do
    pf="$LOG_DIR/${name}.pid"
    if [ -f "$pf" ]; then
      p="$(cat "$pf")"
      if kill -0 "$p" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') Restarting $name (PID $p) for new code" >> "$SYNC_LOG"
        kill "$p" 2>/dev/null || true
      fi
    fi
  done
  sleep 2

  # Round-5 fix: the watchdog script itself also needs the new code. The
  # bash process has already read the old script into memory; `git reset`
  # on disk doesn't retroactively update it. `exec "$0"` replaces this
  # process in place (same PID, so watchdog.pid stays valid) with a fresh
  # invocation of the updated script. Children were just killed above; the
  # fresh watchdog will restart them on the new code.
  echo "$(date '+%Y-%m-%d %H:%M:%S') Re-executing watchdog with new code" >> "$SYNC_LOG"
  exec "$0" "$@"
}

while true; do
  sleep 30
  tick=$((tick + 1))

  # Git sync every 10 ticks (5 min)
  if [ $((tick % 10)) -eq 0 ]; then
    _git_sync
  fi

  # Iterate every daemon — same supervision logic applies uniformly.
  for i in "${!DAEMON_NAMES[@]}"; do
    name="${DAEMON_NAMES[$i]}"
    cmd="${DAEMON_CMDS[$i]}"
    pidfile="$LOG_DIR/${name}.pid"

    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      # Alive — bump healthy ticks, reset restart count after stable uptime.
      HEALTHY_TICKS[$i]=$((HEALTHY_TICKS[$i] + 1))
      if [ "${HEALTHY_TICKS[$i]}" -ge "$HEALTHY_RESET_TICKS" ] && [ "${RESTART_COUNTS[$i]}" -gt 0 ]; then
        echo "[watchdog] $(date -u): $name stable for $HEALTHY_RESET_TICKS ticks — resetting restart count (was ${RESTART_COUNTS[$i]})"
        RESTART_COUNTS[$i]=0
      fi
      continue
    fi

    # Dead — reset healthy streak, maybe restart.
    HEALTHY_TICKS[$i]=0
    if [ "${RESTART_COUNTS[$i]}" -ge "$MAX_RESTARTS" ]; then
      echo "[watchdog] $(date -u): $name max restarts ($MAX_RESTARTS) reached — giving up on $name."
      continue
    fi
    RESTART_COUNTS[$i]=$((RESTART_COUNTS[$i] + 1))
    echo "[watchdog] $(date -u): $name not running — restart #${RESTART_COUNTS[$i]}"
    # shellcheck disable=SC2086  # intentional word-splitting of $cmd
    nohup $cmd >> "$LOG_DIR/${name}.log" 2>&1 &
    echo $! > "$pidfile"
    echo "[watchdog] $(date -u): $name restarted (PID $(cat "$pidfile"))"
  done
done
