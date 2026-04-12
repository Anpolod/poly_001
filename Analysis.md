# Polymarket Sports — Project Analysis

> Last analyzed: 2026-04-05

---

## Project Summary

A two-phase Python data pipeline for Polymarket sports markets:

- **[[phase0_report|Phase 0]]** — one-shot cost analysis scan across all sports markets. Outputs `phase0_results.csv` and `cost_analysis` DB table. Verdicts: `GO` (ratio ≥ 2.0), `MARGINAL` (1.5–2.0), `NO_GO` (< 1.5).
- **Phase 1** — continuous 24/7 collector. Price snapshots every 60s, real-time trades via WebSocket, hourly market rescan.

Stack: Python `asyncio` + `aiohttp` + `asyncpg` + `websockets` + PostgreSQL/TimescaleDB.

---

## Architecture

```
Phase 0: REST APIs → CostAnalyzer → phase0_results.csv + DB (cost_analysis)

Phase 1: REST (snapshots) ──┐
         WebSocket (trades) ─┤→ Normalizer → Repository → DB
         SpikeTracker ───────┘
         MarketDiscovery (rescan every 3600s)
```

### Module Map

| Module | Role |
|--------|------|
| `main.py` | Phase 1 entrypoint — 4 async tasks |
| `cost_analyzer.py` | Phase 0 entrypoint — batch scan, CSV + DB |
| `collector/rest_client.py` | HTTP client (Gamma + CLOB APIs), retries |
| `collector/ws_client.py` | WebSocket with exponential backoff (5s→60s) |
| `collector/market_discovery.py` | Sport/league detection, multi-stage filtering |
| `collector/normalizer.py` | Snapshot normalization, price move calc |
| `collector/spike_tracker.py` | Per-market direction state machine |
| `analytics/cost_analyzer.py` | Core math: taker/maker round-trip costs, ratio |
| `db/repository.py` | asyncpg pool (2–10 conns), idempotent upserts |
| `db/schema.sql` | 7 tables, 2 hypertables (price_snapshots, trades) |

---

## Phase 0 Results

> [!warning] Recommendation: STOP
> `clean_go_pct = 6.1%` which is below the 15% proceed threshold.
> 
> *Proceed to Phase 1 only if ratio_24h > 1.5 on clean GO subset AND clean_go_pct ≥ 15%.*

### Verdict Distribution (111 markets total)

| Verdict | Count | % |
|---------|-------|---|
| GO | 9 | 8.11% |
| MARGINAL | 5 | 4.5% |
| NO_GO | 52 | 46.85% |
| NO_DATA | 45 | 40.54% |

**Clean GO (zero flags): 4 out of 9 GO markets (6.1% of all data markets)**

### By Sport

| Sport | Markets | GO | Clean GO | Ratio Median | % >1.5 |
|-------|---------|----|---------:|-------------:|--------|
| baseball | 18 | 0 | 0 | 0.33 | 33.33% |
| basketball | 31 | 3 | 2 | 1.38 | 37.5% |
| football | 51 | 5 | 2 | 0.0 | 18.0% |
| hockey | 4 | 0 | 0 | 0.0 | 0.0% |
| tennis | 7 | 1 | 0 | 11.62 | 100.0% |

> [!tip] Basketball is the strongest candidate
> Highest clean GO count (2) and median ratio closest to threshold (1.38). Tennis has extreme outlier ratio but zero clean GO markets.

### GO Flag Breakdown

| Flag | Count | % of GO |
|------|-------|---------|
| LOW_VOL | 4 | 44.44% |
| MOVE_EXCEEDS_HALF_PRICE | 3 | 33.33% |
| EXTREME_ODDS | 1 | 11.11% |
| RATIO_OUTLIER | 1 | 11.11% |
| THIN_DEPTH | 0 | 0.0% |

> [!note] Artifact detected
> Market `elc-sot-ips-2026-04-03-ips` (football) has ratio_24h = 68.52 — likely a data error. Investigate before including.

---

## Bug Fixes Applied (Phase 0)

See [[phase0_fixes_report]] for full details.

### Fix 1 — Orderbook index reversed (CRITICAL)
- **Before:** `bids[-1]` / `asks[-1]` → worst prices → spread ≈ 98%
- **After:** `bids[0]` / `asks[0]` → best prices (Polymarket CLOB sort order)
- **Impact:** ~40% of NO_DATA markets should resolve after this fix

### Fix 2 — Price history fidelity too coarse (CRITICAL)
- **Before:** `fidelity=3600` (1h candles) → new markets return 0 data points
- **After:** `fidelity=60` (1m candles) + `interval=1w` fallback
- **Impact:** Remaining NO_DATA should drop below 5%

### Fix 3 — Depth calculation wrong for binary contracts (minor)
- **Before:** `sum(price * size)` — artificially deflated by 5–95%
- **After:** `sum(size)` — size already is dollar notional in binary markets

> [!important] Phase 0 results above were collected BEFORE bug fixes
> Re-run `python cost_analyzer.py --fresh` to get corrected numbers. Expect significantly more GO markets and far fewer NO_DATA.

---

## Task Completion Status

All 18 tasks (T-01 through T-18) are **completed** as of 2026-04-04.

See [[TASKS]] for full details. Summary of key work done:

| Area | Task | What was built |
|------|------|----------------|
| Stability | T-01–T-04 | Config validation, DB error handling, env-var secrets, config-driven thresholds |
| Integration | T-05–T-09 | Backfill CLI, checkpoint/resume, WS config, Slack alerts, analytics CLI tools |
| Testing | T-11–T-13 | 84 tests total (pytest): collector, repository, cost math |
| Code Quality | T-14–T-18 | Docstrings, ruff + mypy, CI/CD, lifecycle monitoring, English translation |

---

## What's Still Missing

From [[PROJECT_STATE]]:

| Area | Status |
|------|--------|
| `analytics/cost_backfill.py` | Orphaned — exists but not integrated into any automated flow |
| `analytics/spike_vs_drift_report.py` | Standalone only, not automated |
| `analytics/timing_analyzer.py` | Standalone only, not automated |
| `alerts/logger_alert.py` | Slack webhooks added, but no email/PagerDuty |
| Web UI / REST API | Missing — all access is CLI/CSV |
| Integration tests | Missing for WS, normalizer in end-to-end scenarios |

---

## Known Risks

### High
- `main.py` reads config keys directly — missing YAML key = `KeyError` crash at startup (mitigated by T-01 config validation)
- `settings.yaml` stores DB password in plaintext — use env-var overrides (`DB_PASSWORD` env var) added in T-04

### Medium
- Phase 0 scan can take many hours for 1,000+ markets at 1 req/s — checkpoint/resume added (T-06), but no ETA display
- `filter_tradeable()` thresholds now configurable (T-03) but defaults still hardcoded as fallbacks — document clearly

### Low
- `_on_spike()` catches generic `Exception` and logs at INFO — spike errors are easy to miss
- No docstrings on test files

---

## Key Decision Points

```
Phase 0 result analysis
       │
       ├─ clean_go_pct ≥ 15% ──→ Proceed to Phase 1
       │
       └─ clean_go_pct < 15% ──→ STOP (current state: 6.1%)
                                  │
                                  └─ BUT: results pre-date bug fixes
                                     Re-run with --fresh first
```

> [!question] Next step
> Run `python cost_analyzer.py --fresh` with the 3 bug fixes applied. If new clean_go_pct ≥ 15% → proceed to Phase 1.

---

## Quick Reference

```bash
# Re-run Phase 0 from scratch
python cost_analyzer.py --fresh

# Phase 1 (continuous)
python main.py

# Analytics
python -m analytics.movement_analyzer --sport basketball
python -m analytics.spike_vs_drift_report --min-snapshots 30

# Tests
make test

# Lint + typecheck
make all
```

---

## Linked Documents

- [[README]] — setup and install guide
- [[PROJECT_STATE]] — detailed architecture and risk register
- [[TASKS]] — all 18 tasks with completion notes
- [[phase0_report]] — full Phase 0 statistical analysis
- [[phase0_fixes_report]] — bug fix details with before/after code
- [[CLAUDE.md]] — Claude Code guidance and debug commands
