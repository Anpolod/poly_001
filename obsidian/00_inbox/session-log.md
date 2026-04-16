# Session Log

> Cross-session activity log. Newest entries at top.
> Updated by Claude Code at the start/end of each work session.

---

## 2026-04-14 (session 4)

- **Mac Mini deployment complete** — бот теперь работает ТОЛЬКО на Mac Mini (192.168.8.208)
  - Postgres.app 2.9.4 (PG16) установлен без Homebrew
  - `db/init_schema.py` запущен — все 12 таблиц созданы (без TimescaleDB — schema.sql обновлён: graceful skip)
  - Python 3.13 venv создан, все зависимости установлены
  - LaunchAgent для автостарта PostgreSQL при перезагрузке
  - Бот запущен в DRY RUN, CLOB balance $104.46, dashboard: http://192.168.8.208:8501
  - MacBook бот остановлен — MacBook теперь только для написания кода
- **T-20 закрыт** — pitcher_signals таблица есть на Mac Mini
- Сохранена память: `project_deployment.md` + `macmini_setup.md`
- Next: T-21 — первый MLB scanner dry run на Mac Mini

---

## 2026-04-14 (session 3)

- **Built MLB Pitcher Scanner** — full implementation:
  - `collector/mlb_data.py` — ESPN API client (schedule, probable pitchers, season stats: ERA/WHIP/K9/W-L)
  - `analytics/mlb_pitcher_scanner.py` — scanner with signal classification, Polymarket matching, DB logging
  - `config/mlb_team_aliases.yaml` — 30 MLB teams, all aliases
  - `db/schema.sql` — added `pitcher_signals` table
  - `config/settings.yaml` — added `mlb_pitcher_scanner` section
  - `trading/bot_main.py` — integrated `_process_pitcher_signal()` + MLB scan in main loop
  - `Makefile` — added `make mlb` and `make mlb-watch` targets
  - `CLAUDE.md` — documented CLI commands
- Updated `TASKS.md` — replaced completed T-01→T-18 with new T-19→T-34 (MLB deploy, optimizations, new strategies, cleanup)
- Updated `Instructions/mac-mini-deploy.md` — added pitcher_signals table, MLB commands
- Next: deploy on Mac Mini → T-20 (create table) → T-21 (first dry run) → T-22 (watch mode 2-3 days)

## 2026-04-14 (session 2)

- Deep analysis of NBA tanking pattern from Telegram posts (83 trades, 78.6% WR)
- Created `obsidian/Strategies/` folder with full strategy catalog
- Rewrote `Instructions/roadmap-4-strategies.md` — 6-phase implementation plan
- Full project audit: 12 analytics modules, 10 trading modules, dashboard, DB, CI

---

## 2026-04-14 (session 1)

- Vault structure built: `INDEX.md`, `00_inbox/`, `Trading/`, `_compiled/concepts/`, `_compiled/connections/`
- 6 raw session digests available in `_compiled/_raw/` (Apr 3 – Apr 12); knowledge base not yet compiled
- Reporter paths confirmed: `Prop Scanner/`, `Calibration/`, `P&L/`, `Trading/`

---
