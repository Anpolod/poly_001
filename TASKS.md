# TASKS.md

> Last updated: 2026-04-14
> Priority: P1 = blocking / P2 = important / P3 = nice-to-have
> Previous tasks (T-01 → T-18): all completed, archived in git history

---

## P1 — MLB Pitcher Scanner (Phase B)

### T-19 · MLB Pitcher Scanner — deploy and validate ✅
**Files:** `collector/mlb_data.py`, `analytics/mlb_pitcher_scanner.py`, `config/mlb_team_aliases.yaml`
**Done:** ESPN API client (schedule + pitcher stats), scanner with signal classification (HIGH/MODERATE/WATCH), DB table `pitcher_signals`, config section, Makefile targets, bot_main integration.

---

### T-20 · Create pitcher_signals DB table on Mac Mini ✅
**Status:** DONE — `db/init_schema.py` ran on Mac Mini 2026-04-14, all 12 tables created
**Command:**
```bash
psql -p 5432 -U polybot -d polymarket -f db/schema.sql
# или только новую таблицу:
psql -p 5432 -U polybot -d polymarket -c "
CREATE TABLE IF NOT EXISTS pitcher_signals (
    id SERIAL PRIMARY KEY,
    scanned_at TIMESTAMPTZ DEFAULT NOW(),
    market_id TEXT NOT NULL,
    game_start TIMESTAMPTZ,
    favored_team TEXT, underdog_team TEXT,
    home_pitcher TEXT, home_era FLOAT,
    away_pitcher TEXT, away_era FLOAT,
    era_differential FLOAT, quality_differential FLOAT,
    current_price FLOAT, drift_24h FLOAT,
    signal_strength TEXT, action TEXT,
    traded BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_pitcher_signals_market ON pitcher_signals (market_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_pitcher_signals_game_start ON pitcher_signals (game_start);
"
```

---

### T-21 · First MLB scanner dry run — verify ESPN data + Polymarket matching ✅
**Status:** DONE — 2026-04-14
- Переключили с ESPN `/athletes` (404 на всех) на MLB Stats API (`statsapi.mlb.com`)
- 30 игр, 50/53 питчеров с ERA/WHIP, 4 HIGH сигнала с реальными Polymarket рынками
- `collector/mlb_data.py` переписан: schedule + stats через MLB Stats API

---

### T-22 · Run MLB scanner in watch mode — накапливать данные ✅
**Status:** RUNNING — запущен 2026-04-14, PID 7588
**Command:** `nohup venv/bin/python -m analytics.mlb_pitcher_scanner --watch --save >> logs/mlb_scanner.log &`
**Добавлен в** `scripts/start_bot.sh` — стартует автоматически при `make start`
**Данные:** 9 сигналов записано в `pitcher_signals` за первый цикл
**Ждать:** 2-3 дня накопления перед тюнингом порогов (T-23)

---

### T-23 · Tune MLB scanner thresholds based on collected data
**Status:** TODO (after T-22)
**Что подкрутить:**
- `min_era_differential` (сейчас 1.0 — может быть слишком низко)
- `high_era_threshold` / `high_quality_threshold` (пороги для HIGH сигнала)
- Price thresholds в `_recommended_action()` (сейчас: BUY если < 0.65)
- Добавить bullpen fatigue factor если данные покажут что это влияет

---

## P2 — Optimizations

### T-24 · Tanking pattern hourly backtest
**Status:** TODO
**Цель:** построить кривую drift по часам для tanking-матчей (T-36h → T-0h). Найти оптимальный entry/exit.
**Как:** расширить `tanking_scanner.py → run_backtest()` — добавить hourly granularity (сейчас только T-24h, T-12h, T-6h, T-2h).

---

### T-25 · P&L аудит — реальный средний profit/loss
**Status:** TODO
**Цель:** посчитать средний лосс на 16 минусовых сделках из 83. Это ключевой вопрос — если средний лосс > средний профит, то 78.6% WR менее впечатляет.
**Как:** query `open_positions WHERE signal_type='tanking' AND status='closed'` → агрегация по pnl_usd.

---

### T-26 · Back-to-back filter for tanking scanner ✅
**Status:** DONE — 2026-04-15
**Файл:** `analytics/tanking_scanner.py`
**Что:** ESPN schedule API детектирует B2B; HIGH→MODERATE, MODERATE→WATCH; флаг в таблице.

---

### T-27 · Dynamic exit trigger ✅
**Status:** DONE — 2026-04-15
**Файл:** `trading/exit_monitor.py`
**Что:** `check_stagnation_exit()` — выход если последние 3 снапшота без движения > 0.5¢. Гарды: held ≥ 30min, game > 2h. Вызывается каждый цикл в bot_main рядом с check_and_exit.

---

## P2 — New Strategies (Phase C-F)

### T-28 · Injury Scanner
**Status:** PLANNED
**Файлы:** `analytics/injury_scanner.py` (новый), DB: `injury_signals`
**Суть:** парсить Rotowire каждые 10 мин, ловить OUT/DOUBTFUL для ключевых игроков (PPG ≥ 15), сигнал на покупку противника.

---

### T-29 · Calibration Trader
**Status:** PLANNED
**Файлы:** `analytics/calibration_signal.py` (новый), DB: `calibration_edges`
**Суть:** автоматизировать favorite-longshot bias. Daily build edge table → online scan vs active markets.

---

### T-30 · Real-time Drift Monitor
**Status:** PLANNED
**Суть:** кросс-спортивный алерт при drift > 4% за 6ч без news event. Overlay поверх других стратегий.

---

### T-31 · Spike Follow
**Status:** PLANNED
**Файлы:** `analytics/spike_signal.py` (новый)
**Суть:** follow smart money после spike events (magnitude ≥ 5¢, ≥ 4 steps).

---

## P3 — Infrastructure & Cleanup

### T-32 · Clean up project root
**Status:** TODO
**Что:** переместить `PROJECT_STATE.md` → `docs/`, `phase0_*.csv/json/md` → `docs/phase0/`, удалить `Untitled.base`. Обновить `.gitignore` если нужно.

---

### T-33 · Add MLB to Streamlit dashboard
**Status:** TODO
**Что:** новая страница "MLB Pitcher Signals" в dashboard — аналогично NBA Tanking page. Таблица из `pitcher_signals`, фильтр по signal_strength, график drift по времени.

---

### T-34 · MLB data collection pipeline
**Status:** TODO (если решим собирать historical данные)
**Что:** добавить MLB market discovery в `main.py` коллектор, чтобы снапшоты MLB рынков записывались так же как NBA. Это даст данные для будущего бэктеста.

---

## Completed (this sprint)

- **T-19** MLB Pitcher Scanner built: `collector/mlb_data.py` + `analytics/mlb_pitcher_scanner.py` + `config/mlb_team_aliases.yaml` + DB table + config + Makefile + bot_main integration
- **T-20** DB schema deployed on Mac Mini (all 12 tables)
- **T-21** MLB scanner validated: switched to MLB Stats API, 50/53 pitchers enriched, 4 HIGH signals found
- **T-22** MLB scanner running in watch mode on Mac Mini, added to start_bot.sh autostart
