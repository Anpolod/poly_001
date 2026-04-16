PYTHON := venv/bin/python
RUFF   := venv/bin/ruff
MYPY   := venv/bin/mypy
PYTEST := venv/bin/pytest

.PHONY: lint format typecheck test fix all dashboard obsidian obsidian-trading scanner-once scanner-daemon tanking tanking-backtest signals trading start stop status logs

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

## Scan upcoming MLB games for pitcher mismatches
mlb:
	$(PYTHON) -m analytics.mlb_pitcher_scanner

## MLB pitcher scanner in watch mode (refresh every 30 min)
mlb-watch:
	$(PYTHON) -m analytics.mlb_pitcher_scanner --watch --save

## Send signal digest to Telegram (prop + tanking)
signals:
	$(PYTHON) scripts/send_signals.py

## Start trading bot (requires .env with POLYGON_PRIVATE_KEY, trading.enabled: true)
trading:
	$(PYTHON) -m trading.bot_main

## Start bot + dashboard + watchdog in background (Mac mini)
start:
	bash scripts/start_bot.sh

## Stop all background services
stop:
	bash scripts/stop_bot.sh

## Show running PIDs and last 20 log lines
status:
	@echo "=== Process status ==="
	@for svc in bot dashboard watchdog; do \
	  pidfile=logs/$$svc.pid; \
	  if [ -f $$pidfile ]; then \
	    pid=$$(cat $$pidfile); \
	    if kill -0 $$pid 2>/dev/null; then \
	      echo "  $$svc: running (PID $$pid)"; \
	    else \
	      echo "  $$svc: DEAD (stale PID $$pid)"; \
	    fi; \
	  else \
	    echo "  $$svc: not started"; \
	  fi; \
	done
	@echo ""
	@echo "=== Last 20 lines (bot.log) ==="
	@tail -20 logs/bot.log 2>/dev/null || echo "  (no log yet)"

## Tail bot log live
logs:
	tail -f logs/bot.log

## Generate Obsidian trading diary + summary
obsidian-trading:
	$(PYTHON) -m analytics.obsidian_reporter --report trading

## Export dashboard data (DB → JSON) once
dashboard-export:
	$(PYTHON) dashboard/export_dashboard_data.py

## Export dashboard data continuously (every 60s)
dashboard-export-watch:
	$(PYTHON) dashboard/export_dashboard_data.py --watch

## Serve HTML dashboard (auto-refreshes from dashboard_data.json)
dashboard-html:
	@echo "Open http://localhost:8080/stats_dashboard.html"
	cd dashboard && $(PYTHON) -m http.server 8080

## Run lint + typecheck + test
all: lint typecheck test
