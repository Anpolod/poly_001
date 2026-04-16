#!/usr/bin/env bash
# start_bot.sh — launch trading bot + dashboard in background
# Usage: ./scripts/start_bot.sh

set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/logs"
mkdir -p "$LOG_DIR"

PYTHON="${PYTHON:-$REPO/venv/bin/python}"
if [ ! -f "$PYTHON" ]; then
  PYTHON="$(which python3)"
fi

# ── Check .env ────────────────────────────────────────────────────────────────
if [ ! -f "$REPO/.env" ]; then
  echo "ERROR: $REPO/.env not found. Create it with POLYGON_PRIVATE_KEY=0x..."
  exit 1
fi

# Round-4 fix: load .env so LOCAL_BIND_IP, POLYGON_PRIVATE_KEY, etc. reach
# every nohup'd child via inherited environment. Previously only
# trading/bot_main.py read .env itself, leaving collector + sidecar daemons
# without the network-binding workaround.
set -a
# shellcheck disable=SC1091
. "$REPO/.env"
set +a

# ── T-45: DB schema preflight runs BEFORE stop_bot.sh ───────────────────────
# Watchdog auto-pulls code every 5 min, but it does NOT run SQL migrations.
# Codex round 9 HIGH finding: the previous ordering (stop_bot → preflight) meant
# that a preflight failure left every daemon already stopped, turning a
# recoverable schema mismatch into an avoidable outage. Reorder: preflight
# must succeed BEFORE we touch any running service. If it fails the
# previously-running daemons keep running untouched; operator applies
# db/schema.sql and retries.
echo "Running DB schema preflight..."
if ! "$PYTHON" "$REPO/scripts/db_preflight.py"; then
  echo "ERROR: DB schema preflight failed — running services left untouched." >&2
  echo "Apply db/schema.sql manually and rerun start_bot.sh." >&2
  exit 1
fi

# ── Kill any already-running instances ───────────────────────────────────────
"$REPO/scripts/stop_bot.sh" 2>/dev/null || true

# ── Start Phase 1 collector ──────────────────────────────────────────────────
# T-34: collector is now a first-class supervised service. It fills
# price_snapshots / trades / spike_events that every downstream scanner reads.
echo "Starting Phase 1 collector..."
nohup "$PYTHON" "$REPO/main.py" \
  >> "$LOG_DIR/collector.log" 2>&1 &
echo $! > "$LOG_DIR/collector.pid"
echo "  Collector PID: $(cat "$LOG_DIR/collector.pid")  (logs → logs/collector.log)"

# ── Start bot ─────────────────────────────────────────────────────────────────
echo "Starting trading bot..."
nohup "$PYTHON" -m trading.bot_main \
  >> "$LOG_DIR/bot.log" 2>&1 &
echo $! > "$LOG_DIR/bot.pid"
echo "  Bot PID: $(cat "$LOG_DIR/bot.pid")  (logs → logs/bot.log)"

# ── Start dashboard ───────────────────────────────────────────────────────────
echo "Starting Streamlit dashboard..."
nohup "$PYTHON" -m streamlit run "$REPO/dashboard/main_dashboard.py" \
  --server.port 8501 \
  --server.headless true \
  --server.address 0.0.0.0 \
  >> "$LOG_DIR/dashboard.log" 2>&1 &
echo $! > "$LOG_DIR/dashboard.pid"
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo "  Dashboard PID: $(cat "$LOG_DIR/dashboard.pid")  → http://$LOCAL_IP:8501"

# ── Start watchdog ────────────────────────────────────────────────────────────
echo "Starting watchdog..."
nohup "$REPO/scripts/watchdog.sh" \
  >> "$LOG_DIR/watchdog.log" 2>&1 &
echo $! > "$LOG_DIR/watchdog.pid"
echo "  Watchdog PID: $(cat "$LOG_DIR/watchdog.pid")"

# ── Start stats exporter + HTML server ───────────────────────────────────────
echo "Starting stats exporter..."
nohup "$PYTHON" "$REPO/dashboard/export_dashboard_data.py" --watch \
  >> "$LOG_DIR/stats_exporter.log" 2>&1 &
echo $! > "$LOG_DIR/stats_exporter.pid"
echo "  Stats Exporter PID: $(cat "$LOG_DIR/stats_exporter.pid")  (logs → logs/stats_exporter.log)"

echo "Starting stats HTML server..."
nohup "$PYTHON" -m http.server 8502 \
  --directory "$REPO/dashboard" \
  >> "$LOG_DIR/stats_server.log" 2>&1 &
echo $! > "$LOG_DIR/stats_server.pid"
echo "  Stats Server PID: $(cat "$LOG_DIR/stats_server.pid")  → http://$LOCAL_IP:8502/stats_dashboard.html"

# ── Start MLB pitcher scanner (watch mode) — gated on config flag ───────────
# T-45: previously this daemon was launched unconditionally. On non-MLB
# deployments (or when the operator disabled it in config) that produced
# unsolicited ESPN traffic and writes into pitcher_signals. Now we respect
# `mlb_pitcher_scanner.enabled` from settings.yaml — same gate that
# trading.bot_main already uses for its in-process scanner helper.
MLB_ENABLED=$("$PYTHON" "$REPO/scripts/config_get.py" mlb_pitcher_scanner.enabled)
if [ "$MLB_ENABLED" = "true" ]; then
  echo "Starting MLB pitcher scanner..."
  nohup "$PYTHON" -m analytics.mlb_pitcher_scanner --watch --save \
    >> "$LOG_DIR/mlb_scanner.log" 2>&1 &
  echo $! > "$LOG_DIR/mlb_scanner.pid"
  echo "  MLB Scanner PID: $(cat "$LOG_DIR/mlb_scanner.pid")  (logs → logs/mlb_scanner.log)"
else
  echo "MLB pitcher scanner disabled (mlb_pitcher_scanner.enabled != true) — skipping."
  # Clean up any stale pid file from a previous enabled run so watchdog
  # doesn't try to supervise a process that was never launched this cycle.
  rm -f "$LOG_DIR/mlb_scanner.pid"
fi

echo ""
echo "All services started. Use 'make status' to check and 'make logs' to tail."
