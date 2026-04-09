# TASKS.md

> Last updated: 2026-04-04 (T-01–T-18 all completed)
> Priority: P1 = blocking / P2 = important / P3 = nice-to-have

---

## P1 — Bugs & Stability risks

~~### T-01 · Config validation at startup~~
~~**File:** `main.py`, `cost_analyzer.py`~~
**Done:** `config/validate.py` — startup validation with fail-fast error list. Called in both `main.py` and `cost_analyzer.py`.

---

~~### T-02 · Wrap Phase 0 DB inserts in try/except~~
~~**File:** `cost_analyzer.py`~~
**Done:** `upsert_market` and `insert_cost_analysis` wrapped in try/except with per-market error logging.

---

~~### T-03 · Expose `filter_tradeable()` thresholds in config~~
~~**File:** `collector/market_discovery.py`~~
**Done:** All thresholds read from `config.filter_tradeable` with fallbacks to `phase1` values.

---

~~### T-04 · Replace plaintext DB password with env-var support~~
~~**File:** `config/settings.example.yaml`, `db/repository.py`~~
**Done:** `DB_HOST/PORT/NAME/USER/PASSWORD` env-var overrides in `repository.py`. Documented in `settings.example.yaml`.

---

## P2 — Completeness & Integration

~~### T-05 · Integrate `cost_backfill.py` into Phase 0 or a CLI command~~
~~**File:** `analytics/cost_backfill.py`~~
**Done:** `--backfill` flag added to `cost_analyzer.py` — runs `cost_backfill.run()` after Phase 0 scan completes. Standalone `python -m analytics.cost_backfill` also documented in CLAUDE.md.

---

~~### T-06 · Add checkpoint/resume to Phase 0 scan~~
~~**File:** `cost_analyzer.py`~~
**Done:** `.phase0_checkpoint.json` persists processed IDs atomically after each market. Resume is automatic on restart; `--fresh` flag forces full rescan.

---

~~### T-07 · Make WS silence threshold configurable~~
~~**File:** `collector/ws_client.py`~~
**Done:** `ws_silence_threshold_sec` read from config (default 60s). Also moved `_HEARTBEAT_INTERVAL` and `_GAP_THRESHOLD_SECONDS` from module-level constants to instance vars reading existing config keys.

---

~~### T-08 · Add real alerting to `alerts/logger_alert.py`~~
~~**File:** `alerts/logger_alert.py`~~
**Done:** Slack Incoming Webhook added (`_slack()` helper via aiohttp). Triggers: Phase 0 complete, collector started, data gap, spike detected, WS reconnect loop (≥3 consecutive attempts). Config key `alerts.slack_webhook_url` in `settings.example.yaml`. `WsClient` gained `on_reconnect` callback + `_reconnect_attempt` counter; wired in `main.py`. Failures are logged as warnings and never propagate.

---

~~### T-09 · Promote analytics scripts to CLI tools~~
~~**Files:** `analytics/spike_vs_drift_report.py`, `analytics/timing_analyzer.py`, `analytics/movement_analyzer.py`~~
**Done:** `movement_analyzer` — `--from-date`, `--to-date`, `--sport`, `--min-snapshots`, `--output`. `spike_vs_drift_report` — `--min-snapshots`, `--output`. `timing_analyzer` — already had full argparse. All documented in CLAUDE.md.

---

~~### T-10 · Remove unused `httpx` dependency~~
~~**File:** `requirements.txt`~~
**Done:** `httpx`, `httpcore`, `h11` removed (all unused; httpcore/h11 were httpx transitive deps).

---

## P2 — Testing

~~### T-11 · Add tests for collector modules~~
~~**Files:** `collector/rest_client.py`, `collector/ws_client.py`, `collector/normalizer.py`, `collector/market_discovery.py`~~
**Done:** `tests/test_collector.py` — 52 tests. Covers: normalizer (spread_pct, price_move 3 formats, snapshot), market_discovery (detect_sport, is_sports_market, liquidity_metrics, all 7 filter_tradeable filters), rest_client (parse_event edge cases, get_orderbook HTTP mocks via aioresponses).

---

~~### T-12 · Add tests for `db/repository.py`~~
~~**File:** `db/repository.py`~~
**Done:** `tests/test_repository.py` — 15 tests with mocked asyncpg pool. Covers: upsert_market params + default status, get_active_markets SQL filters, update_market_status, insert_snapshot ON CONFLICT, insert_trade ON CONFLICT + default trade_id, insert_spike_event, insert_gap, close_gap minutes calculation + SQL filter.

---

~~### T-13 · Migrate tests to pytest~~
~~**Files:** `tests/test_cost_analyzer.py`, `tests/test_standalone.py`~~
**Done:** Both files converted to pytest. `pytest>=8.0` + `pytest-asyncio>=0.24` added to `requirements.txt`. `pyproject.toml` added with `testpaths=tests`, `asyncio_mode=auto`. 17/17 pass.

---

## P3 — Code Quality & Ops

~~### T-14 · Add docstrings to key classes and methods~~
~~**Files:** All collector, analytics, db modules~~
**Done:** Class docstrings added to `Repository`, `RestClient`, `WsClient`, `MarketInfo`, `MarketDiscovery`, `LoggerAlert`, `Collector`. Method docstrings added to all public methods in `repository.py` (12), `rest_client.py` (start, close), plus `send` in `logger_alert.py`. `normalizer.py` and `analytics/cost_analyzer.py` were already complete.

---

~~### T-15 · Add linting and type checking~~
~~**Files:** project root~~
**Done:** `ruff>=0.1`, `mypy>=1.0`, `types-PyYAML`, `types-tabulate` added to `requirements.txt`. `pyproject.toml` extended with `[tool.ruff]` (line-length=120, E/F/W/I/B rules, B905 ignored) and `[tool.mypy]` (python_version=3.10, ignore_missing_imports, explicit_package_bases). `Makefile` created with `lint`, `typecheck`, `test`, `fix`, `all` targets. 38 issues auto-fixed; 9 remaining manually resolved (3 unused vars, 5 type annotation gaps, 6 `# noqa: E501` for unavoidable long strings). `make lint` and `make typecheck` both pass clean.

---

~~### T-16 · Add GitHub Actions CI pipeline~~
~~**File:** `.github/workflows/ci.yml` (new)~~
**Done:** `.github/workflows/ci.yml` — triggers on push/PR to master. Steps: checkout → Python 3.12 → pip cache (keyed on requirements.txt hash) → install → `ruff check` → `mypy` → `pytest -v`. Also added missing `PyYAML==6.0.3` and `tabulate==0.10.0` to `requirements.txt`.

---

~~### T-17 · Add market lifecycle monitoring~~
~~**File:** `collector/market_discovery.py`, `db/repository.py`~~
**Done:** `_discover_markets()` in `main.py` cross-checks active markets vs API on each rescan; marks settled in DB and removes from snapshot loop. Analytics queries filter `status != 'settled'` and `event_start > NOW() - INTERVAL '3 hours'`.

---

~~### T-18 · Translate Ukrainian inline comments to English~~
~~**Files:** Multiple (rest_client.py, ws_client.py, others)~~
**Done:** All Ukrainian docstrings, inline comments, log messages, and print statements translated to English across ws_client.py, rest_client.py, market_discovery.py, normalizer.py, repository.py, alerts/logger_alert.py, analytics/cost_analyzer.py, db/init_schema.py, cost_analyzer.py, main.py. Also removed two unused `Optional` imports surfaced during the pass.

---

## Completed

- **T-01** Config validation at startup (`config/validate.py`)
- **T-02** Phase 0 DB inserts wrapped in try/except
- **T-03** `filter_tradeable()` thresholds read from config
- **T-04** DB env-var overrides (`DB_HOST/PORT/NAME/USER/PASSWORD`)
- **T-05** `cost_backfill.py` інтегровано як `--backfill` флаг у `cost_analyzer.py`
- **T-06** Phase 0 checkpoint/resume (`.phase0_checkpoint.json`, `--fresh` flag)
- **T-07** WS silence threshold configurable (`ws_silence_threshold_sec`); heartbeat + gap threshold also moved to config
- **T-09** Analytics scripts promoted to CLI tools with argparse (`--from-date`, `--to-date`, `--sport`, `--min-snapshots`, `--output`)
- **T-10** `httpx`, `httpcore`, `h11` removed from `requirements.txt`
- **T-13** Tests migrated to pytest — 17/17 pass (`venv/bin/pytest -v`)
- **T-11** Collector tests — 52 tests in `tests/test_collector.py` (normalizer, market_discovery, rest_client). Full suite: **69/69 passed**
- **T-12** Repository tests — 15 tests in `tests/test_repository.py` (mocked asyncpg pool). Full suite: **84/84 passed**
- **T-17** Market lifecycle monitoring (settled detection on rescan, analytics filters)
- **T-18** Ukrainian → English translation (all .py files: docstrings, comments, log messages, print statements)
