# PROJECT_STATE.md

> Last updated: 2026-04-04

---

## What this project does

Polymarket sports data pipeline in two phases:

- **Phase 0** — one-shot batch scan of all sports markets via REST. Computes trading costs and edge ratios, outputs `phase0_results.csv` and populates the `cost_analysis` DB table. Verdict per market: `GO` (ratio ≥ 2.0), `MARGINAL` (1.5–2.0), `NO_GO` (<1.5).
- **Phase 1** — 24/7 continuous collector. Takes price snapshots every 60s, streams real-time trades via WebSocket, rescans markets hourly, detects spikes, logs data gaps.

Backend: PostgreSQL + TimescaleDB. All I/O is async (asyncio + asyncpg + aiohttp).

---

## Architecture

```
Phase 0: REST APIs → CostAnalyzer → phase0_results.csv + DB (cost_analysis)

Phase 1: REST (snapshots) ──┐
         WebSocket (trades) ─┤→ Normalizer → Repository → DB
         SpikeTracker ───────┘
         MarketDiscovery (rescan every 3600s)
```

### Key modules

| Module | Role |
|---|---|
| `main.py` | Phase 1 entrypoint — 4 async tasks: snapshot loop, rescan loop, heartbeat, WebSocket |
| `cost_analyzer.py` | Phase 0 entrypoint — batch REST scan, CSV + DB output |
| `collector/rest_client.py` | HTTP client (Gamma + CLOB APIs), retries, rate-limit backoff |
| `collector/ws_client.py` | WebSocket client — exponential backoff, buffering, REST fallback |
| `collector/market_discovery.py` | Sport/league detection, volume/spread/depth filtering |
| `collector/normalizer.py` | Snapshot normalization, price move calculations |
| `collector/spike_tracker.py` | Per-market state machine — detects consecutive 1¢ price steps |
| `analytics/cost_analyzer.py` | Core math: taker/maker round-trip costs, ratio, verdict |
| `db/repository.py` | asyncpg pool (2–10 conns), idempotent upserts via ON CONFLICT |
| `db/schema.sql` | 7 tables: markets, cost_analysis, price_snapshots*, trades*, spike_events, cost_estimates, data_gaps (* = TimescaleDB hypertable) |

### DB schema overview

| Table | Type | Purpose |
|---|---|---|
| `markets` | regular | Event metadata, token IDs, fee rates |
| `cost_analysis` | regular | Phase 0 results per market |
| `price_snapshots` | hypertable | Time-series mid prices (partitioned by time) |
| `trades` | hypertable | Executed trade log |
| `spike_events` | regular | Detected spike records with reversion tracking |
| `cost_estimates` | regular | Fallback costs for markets skipped by Phase 0 |
| `data_gaps` | regular | Downtime intervals (reason, duration) |

---

## What is finished

- Phase 0 full pipeline: discovery → cost math → CSV → DB insert
- Phase 1 full pipeline: snapshot loop, WebSocket, market rescan, heartbeat, graceful shutdown
- WebSocket: exponential backoff reconnection (5s base → 60s max), buffer flush (10 items or 30s), REST fallback when silent >60s, gap detection (>300s)
- SpikeTracker: per-market direction state machine, 3-step minimum, ±0.2¢ tolerance, flush on WS idle
- MarketDiscovery: sport/league detection by slug and keyword, multi-stage filtering (volume, spread, depth, odds extremes)
- DB layer: all upserts idempotent, connection pool, parameterized queries (no injection risk)
- Config: fully YAML-driven for all core thresholds
- Tests: two standalone test files covering cost math (taker RT, maker RT, ratio, verdict) — all pass
- Docker Compose setup for PostgreSQL + TimescaleDB
- Logging: configurable level/path/rotation

---

## What is NOT finished

| Area | Status | Notes |
|---|---|---|
| `analytics/cost_backfill.py` | Orphaned | Exists but called from nowhere — historical cost backfill not integrated |
| `analytics/spike_vs_drift_report.py` | Exploratory | Standalone batch script, not in any automated flow |
| `analytics/timing_analyzer.py` | Exploratory | Standalone batch script, not automated |
| `analytics/movement_analyzer.py` | Exploratory | Standalone batch script, not automated |
| `alerts/logger_alert.py` | Stub | Console/file logging only — no Slack, email, or webhook |
| Web UI / REST API | Missing | No dashboard, no API — all access is CLI/CSV |
| CI/CD | Missing | No GitHub Actions, no linting config, no mypy |
| Integration tests | Missing | No tests for collector, WebSocket, normalizer, or repository layer |
| Config validation | Missing | No schema check at startup — bad keys cause cryptic `KeyError` |

---

## Known risks

**High**
- `main.py` reads config keys directly (no `.get()` with defaults) — missing or misspelled YAML key crashes startup with `KeyError`, not a helpful message.
- `settings.yaml` stores DB password in plaintext — no env-var fallback or secrets management.

**Medium**
- `cost_analyzer.py` (Phase 0 entrypoint) has no try/except around `repo.insert_cost_analysis()` — a DB error mid-scan silently aborts the entire run.
- `market_discovery.filter_tradeable()` has hardcoded thresholds (`MIN_VOLUME=10_000`, `MID_FAVORITE=0.80`, `0.15/0.85` bounds) that are **not** in `settings.yaml` — operator tuning YAML won't see these filters.
- Phase 0 scan is unbounded: 1,000+ markets at 1s/request = many hours, no checkpoint/resume.

**Low**
- WS silence threshold is fixed at 60s (not configurable) — REST fallback won't fire for short silences in low-volume markets.
- `_on_spike()` callback catches generic `Exception` and logs at INFO level — spike errors are easy to miss.
- `httpx` is in `requirements.txt` but unused (leftover from earlier iteration).
- Ukrainian inline comments throughout — non-Ukrainian speakers need translation.
- No docstrings on most classes/methods.

---

## Stack

| Component | Version |
|---|---|
| Python | 3.x (async/await) |
| aiohttp | 3.13.5 |
| asyncpg | 0.31.0 |
| websockets | 16.0 |
| pandas | 3.0.2 |
| numpy | 2.4.4 |
| PostgreSQL + TimescaleDB | via Docker Compose |

---

## How to run

```bash
# Setup
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config/settings.example.yaml config/settings.yaml  # fill in DB credentials
python db/init_schema.py

# Phase 0 (one-time)
python cost_analyzer.py

# Phase 1 (continuous)
python main.py

# Background
nohup python main.py > logs/stdout.log 2>&1 &
```
