PYTHON := venv/bin/python
RUFF   := venv/bin/ruff
MYPY   := venv/bin/mypy
PYTEST := venv/bin/pytest

.PHONY: lint format typecheck test fix all

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

## Run lint + typecheck + test
all: lint typecheck test
