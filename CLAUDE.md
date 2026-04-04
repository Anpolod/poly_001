# CLAUDE.md

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

# Phase 1 — continuous 24/7 data collection
python main.py

# Background mode
nohup python main.py > logs/stdout.log 2>&1 &
```

There is no formal test runner. The files in `tests/` are exploratory scripts run directly with `python tests/<file>.py`.

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
