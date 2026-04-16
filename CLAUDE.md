# CLAUDE.md

## Session bootstrap (read first)

When you start a new session in this project, do this **before** exploring the codebase:

1. **Read** `obsidian/_compiled/index.md` — compiled knowledge from prior sessions
2. **Read** `obsidian/INDEX.md` (if it exists) — full wiki catalog
3. **Read** `obsidian/00_inbox/session-log.md` (if it exists) — cross-session activity log

The compiled knowledge in `_compiled/` is auto-built from JSONL conversation logs. To refresh it: run `python scripts/compile_kb.py`, then say "compile" to me.

---

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment (Windows)

The venv was created on macOS and does not work on Windows. Use system Python 3.12:
```
C:/Users/siriu/AppData/Local/Programs/Python/Python312/python.exe
```
`psql` is not in PATH — query the DB via Python (asyncpg). DB runs in Docker on port 5433.

## Running

```bash
# Phase 1 — start collector (background)
/c/Users/siriu/AppData/Local/Programs/Python/Python312/python.exe main.py >> logs/stdout.log 2>&1 &

# Phase 0 + immediately backfill cost_estimates from CSV and any existing snapshots
python cost_analyzer.py --backfill

# Force full rescan (ignore checkpoint from a previous partial run)
python cost_analyzer.py --fresh

# Backfill cost_estimates only (without re-running Phase 0 scan)
python -m analytics.cost_backfill

# Phase 1 — continuous 24/7 data collection
python main.py

# Backfill cost_estimates only
/c/Users/siriu/AppData/Local/Programs/Python/Python312/python.exe -m analytics.cost_backfill
```

All analytics modules accept `--help` for full option list:
```bash
PYTHON=/c/Users/siriu/AppData/Local/Programs/Python/Python312/python.exe
$PYTHON -m analytics.movement_analyzer --help
$PYTHON -m analytics.spike_vs_drift_report --help
$PYTHON -m analytics.timing_analyzer --help
$PYTHON -m analytics.backtester --help
$PYTHON -m analytics.calibration_analyzer --help
$PYTHON -m analytics.prop_scanner --help
$PYTHON -m analytics.obsidian_reporter --help
```

Tests are exploratory scripts: `python tests/<file>.py` (no formal test runner).

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

# MLB pitcher scanner — find starting pitcher mismatches
python -m analytics.mlb_pitcher_scanner                           # scan next 48h games
python -m analytics.mlb_pitcher_scanner --min-era-diff 1.5        # only significant mismatches
python -m analytics.mlb_pitcher_scanner --hours 24                # next 24h only
python -m analytics.mlb_pitcher_scanner --watch                   # refresh every 30 min
python -m analytics.mlb_pitcher_scanner --save                    # persist to pitcher_signals table
```

## Architecture

**Phase 0 (`cost_analyzer.py`)** — discovers all Polymarket sports markets via REST, calculates round-trip trading costs and price-move ratios, emits CSV + DB records with verdict:
- **GO**: ratio ≥ 2.0 | **MARGINAL**: 1.5–2.0 | **NO_GO**: < 1.5
- `ratio = (price_move / mid_price * 100) / taker_cost_pct`

**Phase 1 (`main.py`)** — runs continuously: snapshots every 5 min, real-time trades via WebSocket, market rescan every hour, logs data gaps on disconnect.

### Key modules

| Module | Role |
|---|---|
| `collector/rest_client.py` | Async HTTP — Gamma API (events/markets) + CLOB API (orderbooks, fees, history) |
| `collector/ws_client.py` | WebSocket with exponential backoff; buffers trades (flush at 10 items or 30s); callbacks: `on_trade`, `on_spike`, `on_reconnect` |
| `collector/spike_tracker.py` | Per-market spike detector — consecutive same-direction CLOB steps → `spike_finalized` dict |
| `collector/market_discovery.py` | Filters markets by volume, spread, depth |
| `collector/normalizer.py` | Normalizes raw API responses |
| `analytics/cost_analyzer.py` | Cost math: taker RT = `fee_rate*2 + spread + slippage`; maker RT = `spread*adverse_mult - rebate` |
| `alerts/logger_alert.py` | Logs always; POSTs to Slack webhook if configured (failures never crash collector) |
| `db/repository.py` | asyncpg pool (2–10 conns); idempotent upserts via `ON CONFLICT` |

### Data flow

```
Phase 0: REST APIs → CostAnalyzer → CSV + DB (cost_analysis)
Phase 1: REST + WebSocket → Normalizer → Repository (price_snapshots, trades, spike_events)
         └─ MarketDiscovery rescans every 3600s
```

## Database

PostgreSQL 16 + TimescaleDB, port 5433. `price_snapshots` and `trades` are hypertables. All time-range indexes on `(market_id, ts)`.

| Table | Purpose |
|---|---|
| `markets` | Metadata: sport, league, event_start, token IDs, status |
| `cost_analysis` | Phase 0 results: spreads, depths, move ratios, verdict |
| `cost_estimates` | Live-computed costs for markets not in cost_analysis |
| `price_snapshots` | 5-min orderbook snapshots (hypertable) |
| `trades` | Real-time WS trades (hypertable) |
| `spike_events` | Price spikes from SpikeTracker; post-spike prices backfilled by scheduled job |
| `data_gaps` | Snapshot collection interruptions |
| `prop_scan_log` | Prop scanner hits: EV, ROI, outcome |
| `historical_calibration` | Resolved market price history (populated once by `historical_fetcher`) |

## Async patterns

All I/O uses `async/await`. Never instantiate `asyncpg` connections directly — always use `db/repository.py`'s pool. WS callbacks (`on_trade`) run inside the async loop — keep them non-blocking. In async tracebacks the root cause is the innermost `await`, not the surface exception.

## Debug

```python
# Query DB (no psql on Windows)
import asyncio, yaml, asyncpg
async def q():
    cfg = yaml.safe_load(open('config/settings.yaml'))['database']
    conn = await asyncpg.connect(host=cfg['host'], port=cfg['port'],
                                  database=cfg['name'], user=cfg['user'], password=str(cfg['password']))
    rows = await conn.fetch("YOUR QUERY")
    await conn.close()
    return rows
print(asyncio.run(q()))
```

```bash
# Collector status
tail -f logs/collector.log
tasklist | grep python          # check process alive
```
