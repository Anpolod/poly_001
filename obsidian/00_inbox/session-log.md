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

---

## 2026-04-16 — T-36 tanking scanner NO-side fix + 9-round adversarial sprint ship

**Shipped to Mac Mini today:** T-28..T-45 (18 tickets, commit 60d8c97) + T-36 (this session).

### Adversarial review saga
- 9 rounds of `/codex:adversarial-review` against the trading bot
- 23 bugs caught: 1 CRITICAL + 13 HIGH + 9 MED, all fixed with regression tests
- Trend: 4 → 3 → 2 → 2 → 3 → 3 → 2 → 2 → 2 (stabilized at 2/round, not converging to zero)
- Every round's output translated to `obsidian/00_inbox/codex-adversarial-review-2026-04-16-round{N}.md`
- **Meta-lesson:** the last 3 rounds found bugs *in the fixes themselves* (T-42 preflight was broken out of the gate in T-43; T-43 preflight's REQUIRED_TABLES missed boot-time writers in T-44; T-44 watchdog killed daemons before verifying schema in T-45). Every new layer needs its own bridge test — we've been systematically adding static regression tests that close each bug class.

### Deploy operational notes
- Mac Mini `open_positions` column drift on first pull: preflight correctly detected missing `exit_order_id`, returned exit code 4 with ALTER statement. Applied manually.
- An orphan Streamlit (PID 12479 from Wed) held port 8501 → new dashboard burned `MAX_RESTARTS=10` budget in watchdog → "giving up". Killed orphan + manually relaunched dashboard. T-39 `HEALTHY_RESET_TICKS` will reset the counter after 5 min healthy uptime.
- Old watchdog (pre-T-42) at the time of auto-pull had no preflight, no kill-children, no exec → pulled new code but no process restarted. Had to manually kill orphan watchdog (PID 13590) + run fresh `start_bot.sh`. One-shot migration debt, future pulls use T-44 preflight gate.

### Background job running on Mac Mini
- `historical_fetcher` (PID 58977) launched ~14:34 UTC, full fetch (~133k markets). When it completes, run `venv/bin/python -m analytics.calibration_signal --build` to populate `calibration_edges`. Until then the calibration_trader scanner emits zero signals.

### What's still open
- **T-22 → T-23** MLB threshold tuning — blocked on data collection (scanner needs weeks of `pitcher_signals` to tune)
- **T-24** tanking hourly backtest — 1-2h work, not blocked
- **T-25** P&L audit — blocked on closed tanking positions (bot is dry_run, no real fills yet)
- **T-32** project root cleanup — cosmetic
- **T-33** MLB in Streamlit — partially done

### Still not settled
- Adversarial review trend didn't converge to zero in 9 rounds. If we keep going, round 10+ likely finds more — but probably diminishing-value polish at this point.
