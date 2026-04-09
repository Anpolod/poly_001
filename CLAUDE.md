# CLAUDE.md

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

# Phase 0 — one-time cost analysis
/c/Users/siriu/AppData/Local/Programs/Python/Python312/python.exe cost_analyzer.py
/c/Users/siriu/AppData/Local/Programs/Python/Python312/python.exe cost_analyzer.py --backfill
/c/Users/siriu/AppData/Local/Programs/Python/Python312/python.exe cost_analyzer.py --fresh

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
