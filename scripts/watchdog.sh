#!/usr/bin/env bash
# watchdog.sh — supervise every long-running daemon launched by start_bot.sh,
# restart them on crash, restart them after git pull so deployed code actually
# runs. Checks every 30s; syncs git every 5 min (every 10 ticks).
#
# Round-4 fix: supervision now covers all 6 daemons (was just bot + collector),
# closing the version-skew gap codex flagged. Each process has its own
# restart counter + HEALTHY_RESET_TICKS reset so routine deploys don't burn
# the MAX_RESTARTS budget.
#
# T-46 fix: bash-in-memory staleness guard. A running watchdog keeps the
# bytes of watchdog.sh it read at startup, even after `git pull` replaces
# the file on disk. If `_git_sync` somehow doesn't hit its own kill+exec
# path (race condition, pre-T-42 version of this script still in memory,
# etc.), the watchdog and all supervised daemons can run stale code
# indefinitely. The mtime check in the main loop below self-execs the
# watchdog whenever scripts/watchdog.sh changes on disk — belt-and-suspenders
# alongside the kill+exec in `_git_sync`.

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/logs"
SYNC_LOG="$LOG_DIR/git_sync.log"
MAX_RESTARTS=10
HEALTHY_RESET_TICKS=10   # 10 ticks × 30s = 5 min of uptime earns the counter back
tick=0

# T-46: record mtime of this script at startup so the main loop can detect
# on-disk changes and self-reload. `stat -f %m` is macOS syntax; fall back
# to `stat -c %Y` for Linux. If neither works, we print "0" and the main-loop
# guard (below) will skip the check rather than spin.
_script_mtime() {
  stat -f %m "$REPO/scripts/watchdog.sh" 2>/dev/null \
    || stat -c %Y "$REPO/scripts/watchdog.sh" 2>/dev/null \
    || echo 0
}
WATCHDOG_START_MTIME=$(_script_mtime)

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

echo "[watchdog] started — supervising ${#DAEMON_NAMES[@]} daemons every 30s, git sync every 5 min (startup_mtime=$WATCHDOG_START_MTIME)"

# ────────────────────────────────────────────────────────────────────────────
# T-46: shared helper — preflight, kill all supervised children, exec self.
# Called by `_git_sync` (after successful pull+preflight) and by the main-loop
# mtime check (when watchdog.sh changed on disk but kill+exec was skipped).
#
# Returns 1 ONLY if preflight fails. On success, exec replaces the process
# and this function does not return.
# ────────────────────────────────────────────────────────────────────────────
_preflight_then_kill_and_exec() {
  local reason="$1"
  if [ -f "$REPO/scripts/db_preflight.py" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Running DB schema preflight ($reason)" >> "$SYNC_LOG"
    if ! "$PYTHON" "$REPO/scripts/db_preflight.py" >> "$SYNC_LOG" 2>&1; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') ⚠️  SCHEMA PREFLIGHT FAILED ($reason) — daemons keep running on old code" >> "$SYNC_LOG"
      return 1
    fi
  fi

  # Preflight passed — kill every supervised daemon so the post-exec watchdog
  # restarts them on the new code. Children were working moments ago, so we
  # use SIGTERM to let graceful shutdown run first.
  for name in "${DAEMON_NAMES[@]}"; do
    pf="$LOG_DIR/${name}.pid"
    if [ -f "$pf" ]; then
      p="$(cat "$pf")"
      if kill -0 "$p" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') Restarting $name (PID $p) for new code ($reason)" >> "$SYNC_LOG"
        kill "$p" 2>/dev/null || true
      fi
    fi
  done
  sleep 2

  # Round-5 fix: replace THIS bash process with a fresh invocation of
  # watchdog.sh — the on-disk version, not the one still in memory.
  # Same PID (watchdog.pid stays valid). Children were just killed above;
  # the fresh watchdog picks them up dead and restarts them on new code.
  echo "$(date '+%Y-%m-%d %H:%M:%S') Re-executing watchdog with new code ($reason)" >> "$SYNC_LOG"
  exec "$0" "$@"
}

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

  # T-44 rollback-on-preflight-fail is still inline here because the rollback
  # step (git reset back to PREV_HEAD) is pull-specific — the main-loop mtime
  # check has no meaningful rollback target.
  if ! _preflight_then_kill_and_exec "after pull"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ⚠️  Rolling back to $PREV_HEAD; next tick will re-attempt pull" >> "$SYNC_LOG"
    git reset --hard "$PREV_HEAD" >> "$SYNC_LOG" 2>&1
    echo "$(date '+%Y-%m-%d %H:%M:%S') Rollback done. Operator: apply db/schema.sql, next tick will re-attempt pull." >> "$SYNC_LOG"
    # Leave children untouched. Return WITHOUT exec — next sync tick retries
    # the fetch/preflight cycle. Rollback puts us back on PREV_HEAD so the
    # next `git rev-parse HEAD != origin/master` check fires again.
    return
  fi
  # _preflight_then_kill_and_exec does not return on success (it exec's).
  # Nothing after this point runs on the success path.
}

while true; do
  sleep 30
  tick=$((tick + 1))

  # Git sync every 10 ticks (5 min)
  if [ $((tick % 10)) -eq 0 ]; then
    _git_sync
    # After _git_sync (whether it pulled, rolled back, or no-op'd), refresh
    # the baseline so the mtime check below doesn't re-trigger on a pull
    # that already went through the preflight+kill+exec path.
    WATCHDOG_START_MTIME=$(_script_mtime)
  fi

  # T-46: detect disk changes that the `_git_sync` path missed. Primary case:
  # the bash process running this script is from a version that predates the
  # current kill+exec logic, so even a successful pull never triggered the
  # self-reload. Secondary case: operator scp'd a new watchdog.sh without a
  # git push. In both, mtime of the on-disk file changes; we kill children
  # and exec ourselves so the new code takes effect. Git reset --hard updates
  # mtime even on unchanged files, so this fires after any pull, which is
  # exactly the safety net we want — a superfluous re-exec is cheap.
  current_mtime=$(_script_mtime)
  if [ "$current_mtime" != "$WATCHDOG_START_MTIME" ] \
      && [ "$current_mtime" != "0" ] \
      && [ "$WATCHDOG_START_MTIME" != "0" ]; then
    echo "[watchdog] $(date -u): watchdog.sh changed on disk ($WATCHDOG_START_MTIME → $current_mtime) — reloading"
    if ! _preflight_then_kill_and_exec "mtime change"; then
      # Preflight failed. Don't keep retrying every 30s in a tight loop:
      # accept the new mtime as the baseline so the next iteration is quiet.
      # If operator fixes the schema, the following `_git_sync` tick will
      # either no-op (HEAD == origin) or pull-then-restart; either way we
      # recover without manual intervention.
      WATCHDOG_START_MTIME="$current_mtime"
    fi
    # _preflight_then_kill_and_exec does not return on success.
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
