# CLAUDE.md

## Session bootstrap (read first)

When you start a new session in this project, do this **before** exploring the codebase:

1. **Read** `obsidian/_compiled/index.md` — compiled knowledge from prior sessions
2. **Read** `obsidian/INDEX.md` (if it exists) — full wiki catalog
3. **Read** `obsidian/00_inbox/session-log.md` (if it exists) — cross-session activity log

The compiled knowledge in `_compiled/` is auto-built from JSONL conversation logs. To refresh it: run `python scripts/compile_kb.py`, then say "compile" to me.

---

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config/settings.example.yaml config/settings.yaml  # then fill in DB credentials and API URLs
python db/init_schema.py
```

## Running

```bash
# Phase 0 — one-time cost analysis scan (generates phase0_results.csv and populates DB)
python cost_analyzer.py

# Phase 0 + immediately backfill cost_estimates from CSV and any existing snapshots
python cost_analyzer.py --backfill

# Force full rescan (ignore checkpoint from a previous partial run)
python cost_analyzer.py --fresh

# Backfill cost_estimates only (without re-running Phase 0 scan)
python -m analytics.cost_backfill

# Phase 1 — continuous 24/7 data collection
python main.py

# Background mode
nohup python main.py > logs/stdout.log 2>&1 &
```

There is no formal test runner. The files in `tests/` are exploratory scripts run directly with `python tests/<file>.py`.

## Dashboard

```bash
# Start the Streamlit monitoring dashboard (http://localhost:8501)
streamlit run dashboard/main_dashboard.py
make dashboard

# Pages: Overview | Markets | Movement Alerts | Cost Analysis | NBA Tanking | System Health
```

## Analytics CLI

```bash
# Movement analyzer — early GO candidates
python -m analytics.movement_analyzer
python -m analytics.movement_analyzer --sport basketball
python -m analytics.movement_analyzer --from-date 2026-04-01 --to-date 2026-04-05
python -m analytics.movement_analyzer --min-snapshots 20 --output report.csv

# Spike vs Drift — classify markets by movement type
python -m analytics.spike_vs_drift_report
python -m analytics.spike_vs_drift_report --min-snapshots 30 --output drift.csv

# Timing analyzer — per-market row-by-row chart
python -m analytics.timing_analyzer --market nba-det-phi-2026-04-04
python -m analytics.timing_analyzer --market nba-det-phi --hours 24 --summary-only

# Backtester — replay price_snapshots with drift/reversion signals
python -m analytics.backtester
python -m analytics.backtester --signal drift --threshold 0.03 --hold 60

# Historical calibration fetcher — populate historical_calibration table (run once)
python -m analytics.historical_fetcher          # full fetch (~133k markets)
python -m analytics.historical_fetcher --limit 500 --no-skip   # test run

# Calibration analyzer — pre-game pricing vs actual outcomes
python -m analytics.calibration_analyzer                    # uses historical_calibration table
python -m analytics.calibration_analyzer --sport basketball
python -m analytics.calibration_analyzer --market-type points --window 12h
python -m analytics.calibration_analyzer --output calibration.csv

# Player prop scanner — find positive-EV NBA prop markets (run 6-12h before tip-off)
python -m analytics.prop_scanner
python -m analytics.prop_scanner --min-ev 0.05 --hours 12
python -m analytics.prop_scanner --watch              # auto-refresh every 5 min (no DB)

# Prop scanner daemon — persistent mode: logs to DB, Slack alerts, auto-resolves outcomes
python -m analytics.prop_scanner --daemon --once      # single cycle (test)
python -m analytics.prop_scanner --daemon             # runs indefinitely (every 5 min)
make scanner-daemon                                    # background (logs → logs/scanner.log)

# Obsidian reports — generate markdown files in obsidian/ for review in Obsidian
python -m analytics.obsidian_reporter                 # all reports (daily, P&L, calibration)
python -m analytics.obsidian_reporter --date 2026-04-05
python -m analytics.obsidian_reporter --report pnl
make obsidian

# Tanking scanner — detect motivated vs. tanking NBA matchups (end-of-season edge)
python -m analytics.tanking_scanner                           # scan next 48h games
python -m analytics.tanking_scanner --min-differential 0.6    # only HIGH+ signals
python -m analytics.tanking_scanner --hours 24                # next 24h only
python -m analytics.tanking_scanner --backtest                # validate on collected data
python -m analytics.tanking_scanner --watch                   # refresh every 30 min
python -m analytics.tanking_scanner --save                    # persist to tanking_signals table
make tanking                                                   # convenience alias
make tanking-backtest
```

## Architecture

The project has two distinct phases:

**Phase 0 (`cost_analyzer.py`)** — discovers all Polymarket sports markets via REST, calculates round-trip trading costs and price-move ratios, and emits a CSV + DB records with a verdict per market:
- **GO**: ratio ≥ 2.0
- **MARGINAL**: 1.5–2.0
- **NO_GO**: < 1.5

where `ratio = (price_move / mid_price * 100) / taker_cost_pct`

**Phase 1 (`main.py`)** — runs continuously: takes price snapshots every 5 min, captures real-time trades via WebSocket, rescans markets every hour, and records data gaps on disconnect.

### Key modules

| Module | Role |
|---|---|
| `collector/rest_client.py` | Async HTTP client for Gamma API (events/markets) and CLOB API (orderbooks, fees, history) |
| `collector/ws_client.py` | WebSocket subscription with exponential backoff (5s base → 60s max); accepts `on_trade` callback |
| `collector/market_discovery.py` | Filters markets by volume, spread, and depth |
| `collector/normalizer.py` | Normalizes raw API responses |
| `analytics/cost_analyzer.py` | Core cost math: taker RT = `(fee_rate*2 + spread + slippage)`, maker RT = `(spread*adverse_mult - rebate)` |
| `db/repository.py` | asyncpg connection pool (2–10 conns); idempotent upserts via `ON CONFLICT` |
| `db/schema.sql` | TimescaleDB schema — `price_snapshots` and `trades` are hypertables partitioned by time |

### Data flow

```
Phase 0: REST APIs → CostAnalyzer → CSV + DB (cost_analysis table)
Phase 1: REST + WebSocket → Normalizer → Repository (price_snapshots, trades, data_gaps)
         └─ MarketDiscovery rescans every 3600s
```

## Configuration (`config/settings.yaml`)

Key thresholds to know:

```yaml
phase0:
  ratio_go_threshold: 2.0
  ratio_marginal_threshold: 1.5
  min_volume_24h: 5000
phase1:
  snapshot_interval_sec: 300
  market_rescan_interval_sec: 3600
  min_volume_24h: 10000
  max_spread: 0.03
  min_depth: 1000
collector:
  ws_reconnect_base_delay: 5
  ws_reconnect_max_delay: 60
  gap_threshold_minutes: 5
```

## Database

PostgreSQL + TimescaleDB. Core tables: `markets`, `cost_analysis`, `price_snapshots` (hypertable), `trades` (hypertable), `data_gaps`. Indexes are on `(market_id, ts)` for time-range queries.

## Async patterns

Every I/O function uses `async/await`. DB access always goes through `db/repository.py`'s connection pool — never instantiate `asyncpg` connections directly. WS callbacks (`on_trade`) are sync functions called inside an async event loop; keep them non-blocking. When reading async tracebacks, the root cause is usually the innermost `await` line, not where the exception surfaces.

## Debug commands

```bash
# Tail live collector logs
tail -f logs/collector.log

# Check how many snapshots have been collected per market (last 24h)
psql -d <dbname> -c "SELECT market_id, COUNT(*) FROM price_snapshots WHERE ts > NOW() - INTERVAL '24h' GROUP BY market_id ORDER BY COUNT DESC LIMIT 20;"

# Check for recent data gaps
psql -d <dbname> -c "SELECT * FROM data_gaps ORDER BY started_at DESC LIMIT 10;"

# Verify API reachability
python -c "import asyncio, aiohttp; asyncio.run(aiohttp.ClientSession().get('https://gamma-api.polymarket.com/markets?limit=1').close())"
```
