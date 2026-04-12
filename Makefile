PYTHON := venv/bin/python
RUFF   := venv/bin/ruff
MYPY   := venv/bin/mypy
PYTEST := venv/bin/pytest

.PHONY: lint format typecheck test fix all dashboard obsidian scanner-once scanner-daemon tanking tanking-backtest

## Start Streamlit dashboard (http://localhost:8501)
dashboard:
	venv/bin/streamlit run dashboard/main_dashboard.py --server.port 8501

## Run ruff linter (no changes)
lint:
	$(RUFF) check .

## Run mypy type checker
typecheck:
	$(MYPY) . --exclude venv

## Run full test suite
test:
	$(PYTEST) -v

## Auto-fix all ruff-fixable issues
fix:
	$(RUFF) check . --fix
	$(RUFF) format .

## Generate Obsidian markdown reports (daily log, P&L, calibration)
obsidian:
	$(PYTHON) -m analytics.obsidian_reporter

## Run prop scanner daemon (one cycle, for testing)
scanner-once:
	$(PYTHON) -m analytics.prop_scanner --daemon --once

## Run prop scanner daemon in background (logs to logs/scanner.log)
scanner-daemon:
	nohup $(PYTHON) -m analytics.prop_scanner --daemon > logs/scanner.log 2>&1 &

## Scan upcoming NBA games for tanking pattern
tanking:
	$(PYTHON) -m analytics.tanking_scanner

## Run tanking backtest on collected snapshots
tanking-backtest:
	$(PYTHON) -m analytics.tanking_scanner --backtest

## Run lint + typecheck + test
all: lint typecheck test
