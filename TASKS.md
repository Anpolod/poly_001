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

### T-28 · Injury Scanner ✅
**Status:** DONE — 2026-04-15
**Файлы:** `analytics/injury_scanner.py`, `db/schema.sql` (`injury_signals`), `trading/bot_main.py`, `config/settings.yaml`
**Что сделано:**
- Парсит Rotowire lineups через BeautifulSoup по новому DOM (`div.lineup` → `ul.lineup__list.is-home/is-visit` → `li.lineup__player[title]`)
- Hard-coded NBA abbr → canonical map (Rotowire отдаёт только 3-буквенные аббревиатуры)
- Матчит injured team с upcoming NBA markets через существующий `find_upcoming_nba_markets`
- Генерирует `InjurySignal(injured_team, healthy_team, action='BUY', ...)` если `current_price < max_entry_price` (default 0.85)
- Dedupe в `injury_signals` через 24h lookback на (market_id, player_name, status)
- Интегрирован в `bot_main.run_loop` — alert-only (6h dedupe через `_injury_alerted`), собственный 10-мин cadence через `_last_injury_scan`
- Dry-run 2026-04-15 нашёл 6 Rotowire reports → 2 BUY signals (Orlando @ 0.465 с Embiid OUT, LAC @ 0.335 с Post OUT)
**TODO next session:** создать `injury_signals` таблицу на Mac Mini (`psql -f db/schema.sql`)

---

### T-29 · Calibration Trader ✅
**Status:** DONE — 2026-04-15
**Файлы:** `analytics/calibration_signal.py`, `db/schema.sql` (`historical_calibration` + `calibration_edges`), `trading/bot_main.py`, `config/settings.yaml`
**Что сделано:**
- Новый модуль с двумя режимами: `--build` (offline: historical_calibration → buckets → calibration_edges) и `scan()` (online: active markets vs edges)
- 5 fixed price buckets ([0.0-0.30), [0.30-0.40), [0.40-0.60), [0.60-0.70), [0.70-1.01)) — прямой SQL lookup `price_lo <= mid < price_hi`
- Confidence grading: HIGH n≥50 / MEDIUM n≥20 / LOW n≥3
- Добавил `historical_calibration` в schema.sql (раньше таблица использовалась в 7 файлах, но не была формально определена)
- Интегрировано в `bot_main` как `_run_calibration_scan` — alert-only, cadence 60 мин, 12h dedupe на market_id
- Config keys: `calibration_trader.enabled/scan_interval_min/min_edge_pct/min_confidence/hours_window/max_signals`
- Sanity test (400 synthetic rows с заданным bias): обнаружил heavy dog +6.01% underpriced, heavy fav -10.39% overpriced, neutral band ~0 — bias detection работает
- Live scan против реального `markets` table нашёл 1 сигнал `BUY_YES nba-gsw-lac-2026-04-15`
**TODO next session:**
1. На Mac Mini применить новый schema.sql → создать `historical_calibration` и `calibration_edges`
2. Запустить `python -m analytics.historical_fetcher` (многочасовой job) — наполнить historical_calibration
3. `python -m analytics.calibration_signal --build` — построить реальные edges

---

### T-30 · Real-time Drift Monitor ✅
**Status:** DONE — 2026-04-15
**Файлы:** `analytics/drift_monitor.py`, `db/schema.sql` (`drift_signals`), `trading/bot_main.py`, `config/settings.yaml`
**Что сделано:**
- Кросс-спортовый scanner: `JOIN markets m JOIN LATERAL (latest snapshot) JOIN LATERAL (snapshot at now-6h ±30min)`
- Threshold default 4% drift over 6h lookback, configurable per cycle
- **has_spike флаг** — cross-check vs `spike_events` за тот же window. Кандидаты с has_spike=true не алертятся в Telegram, но пишутся в БД (microstructure noise)
- Sort: largest |drift_pct| первым, has_spike push'ит ниже
- bot_main integration: `_run_drift_scan` cadence 15 мин, 6h Telegram dedupe per market_id
- action='WATCH' — drift сам по себе не tradeable, это flag для human review против news/scoreboards
- Dry-run против Mac Mini DB: 0 сигналов (markets стабильны в текущем 6h окне) — graceful path работает

---

### T-31 · Spike Follow ✅
**Status:** DONE — 2026-04-15
**Файлы:** `analytics/spike_signal.py`, `db/schema.sql` (`spike_signals`), `trading/bot_main.py`, `config/settings.yaml`
**Что сделано:**
- Polling-based v1 (не WS callback): scanner читает `spike_events` за последние 30 мин с magnitude ≥ 5¢, n_steps ≥ 4
- JOIN с `markets` для game_start + sport, фильтр `min_hours_to_game >= 1.0`
- **Dedupe by spike_event_id** — один signal на event навсегда (FK in DB + in-memory set in bot_main)
- bot_main integration: `_run_spike_scan` cadence 5 мин, in-memory `_spike_alerted` set capped at 500
- entry_price = end_price спайка (точка где спайк остановился)
- action='BUY' но direction-only — YES/NO mapping для правильного token side ждёт T-35 (`resolve_team_token_id`)
- Dry-run против Mac Mini: **9,964 spike_events в БД, найдено 30 валидных сигналов** (NHL преимущественно, mag 0.03-0.04, steps 3-4). Cleanup test rows после теста
**Decision: polling vs WS callback** — выбран polling для консистентности с другими scanner'ами и чтобы не трогать `ws_client.py`. Latency cost ≤ 1 scan_interval (5 мин), приемлемо для 1-2h hold window. Real-time callback можно добавить в v2 если будет ROI.

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

### T-35 · Pre-live trading audit fixes (3× P1 from codex review) ✅
**Status:** DONE — 2026-04-16
**Выполнение:** 3 P1 бага пофикшены, 15 unit-тестов написаны, live verification подтвердила что Lakers в "Rockets vs. Lakers" сидят на NO side.

**Что сделано:**

1. **P1.1 — stop-loss/take-profit exit flow** (`trading/risk_guard.py` + `trading/order_poller.py`)
   - Новый state `fill_status='exit_pending'` + колонка `exit_order_id TEXT` в `open_positions`
   - `risk_guard` после SELL вызывает `mark_exit_pending(position_id, sell_order_id, reason)` вместо `close_position`
   - `order_poller` получил новую ветку `_handle_exit_pending`: MATCHED → `close_position(actual_exit_price)`, CANCELLED/UNMATCHED → `mark_exit_failed` (revert → filled, retry на следующий tick)
   - Rejected SELL (no order_id) оставляет позицию filled и Telegram alert с ошибкой — не теряет shares

2. **P1.2 — injury_scanner healthy_team price** (`analytics/injury_scanner.py`)
   - Добавлен `healthy_side: Optional[str]` в `InjurySignal` dataclass
   - `build_injury_signals` вызывает `resolve_team_token_side(pool, market_id, healthy_team, aliases)` и инвертирует цену в `1 - yes_mid` если `side == 'NO'`
   - Signals, где side нельзя определить, **скипаются** (не alert с неверной ценой)

3. **P1.3 — MLB pitcher favored team side** (`trading/bot_main.py`)
   - `_process_pitcher_signal` получил параметр `mlb_aliases`, resolve происходит **early** (до entry_filter)
   - Все downstream расчёты (entry_filter, position sizing, Telegram alert, executor.buy) используют `favored_price` и `exec_token_id`, а не YES-только
   - `mlb_aliases` поднят из try-блока в scope `run_loop` чтобы `_process_pitcher_signal` его видел

**Общая инфраструктура:**
- Новая функция `resolve_team_token_side(pool, market_id, team, aliases) -> (token_id, 'YES'|'NO')` в [trading/position_manager.py](trading/position_manager.py)
- Pure-logic helper `_resolve_yes_no_teams_from_text` — position-based вместо alias-length
- Новые helpers: `mark_exit_pending`, `mark_exit_failed`, `ensure_exit_order_id_column`
- Колонка `open_positions.exit_order_id TEXT` в [db/schema.sql](db/schema.sql) + one-time ALTER TABLE в `stop_loss_monitor` startup

**Тесты** ([tests/test_resolve_team_token.py](tests/test_resolve_team_token.py)):
- 15 pytest тестов, все проходят за 0.06s
- Покрыты: substring-trap (`nets`/`hornets`), position-override (`Celtics vs Lakers` flip), full names, slug fallback, missing market, missing tokens, unknown team
- Regression test risk_guard: 33 existing tests по-прежнему passing

**Live verification против Mac Mini DB:**
- Запрос `resolve_team_token_side` на реальном рынке `nba-hou-lal-2026-04-18` ("Rockets vs. Lakers") корректно вернул `Lakers → NO` — именно эта штука баганулась бы в injury_scanner v1
- `build_injury_signals` прошёл dry-run без errors, 1 Rotowire report, 0 matched markets (graceful path)
- 48/48 unit tests passing

**Не сделано (out-of-scope, новая мини-задача T-36):**
- tanking_scanner миграция на `resolve_team_token_side` — латентная та же проблема, но не критична; отдельная задача  → **DONE в T-36 (см. ниже)**

---

### T-36 · Tanking scanner NO-side latent fix ✅
**Status:** DONE — 2026-04-16
**Источник:** latent follow-up от T-35 (MLB NO-side execution fix); round 9 post-ship catch-up
**Время:** ~25 мин + 2 новых теста (всего 223 pass)

**Проблема:**
`analytics/tanking_scanner.py` хранил `current_price = m["current_price"]` предполагая что motivated_team всегда на YES стороне. `_process_tanking_signal` в bot_main'е вызывал `_get_token_id` который blindly возвращает `token_id_yes`. Если motivated team на NO стороне Polymarket'а:
- bot buyит token_id_yes по YES-side price logic против underdog'а
- `open_position(..., side='YES')` (hardcoded до T-38) пишет wrong side
- dashboard показывает wrong side

NBA markets обычно YES-side, но bug latent — при любом NO-side motivated матче молчаливая ошибка execution.

**Fix (mirror T-35 + T-41 MLB pattern):**

1. **Scanner** ([analytics/tanking_scanner.py](analytics/tanking_scanner.py#L440)):
   - `TankingSignal.motivated_side: Optional[str]` field
   - Scan loop вызывает `resolve_team_token_side(pool, market_id, motivated, aliases)`
   - Если `motivated_side == "NO"` → inverts `current_price = 1 - yes_mid` и `price_24h = 1 - yes_24h`
   - Skipит markets где side не резолвится — безопаснее misleading data
   - Import `resolve_team_token_side` локальный (внутри функции) чтобы избежать circular dep `analytics → trading`

2. **Execution** ([trading/bot_main.py::_process_tanking_signal](trading/bot_main.py#L246)):
   - Новый параметр `aliases: dict[str, str] | None = None`
   - Когда aliases передан — resolves token + side via `resolve_team_token_side`, сравнивает с `signal.motivated_side` (scanner-time), abort на mismatch
   - Передаёт `side=motivated_side` в `open_position` + `send_order_confirmation`
   - Extra info в Telegram alert включает `Side: {motivated_side}` + notes в DB
   - Call site обновлён передавать `aliases=aliases` (уже loaded via `load_aliases()` at startup)

3. **tanking_signals таблица** — НЕ migrated (no new column). `motivated_side` хранится только in-memory для execution path. DB audit trail через `open_positions.side` уже работает правильно.

**Новые тесты** ([tests/test_position_manager_records.py](tests/test_position_manager_records.py) — 2 новых):
- `test_process_tanking_signal_uses_signal_price_for_no_side_directly` — NO-side signal с `current_price=0.72` → `executor.buy` получает 0.72/0.73 (НЕ 0.28 = 1-0.72), `open_position` получает `side="NO"` и `token_id="NO_TOKEN_ID"`
- `test_process_tanking_signal_aborts_on_side_mismatch` — scanner-time side ≠ live-resolve side → `executor.buy` не вызывается, exposure=0

**Verification:**
- **223/223 Python tests** pass (2 новых + 221 после T-45)
- Pattern consistent с MLB: scanner side-corrects → execution trusts signal + verifies live

---

### T-55 · Persist tanking signals → enable future backtest ✅
**Status:** DONE — 2026-04-21
**Источник:** T-54 post-mortem обнаружил что `tanking_signals` таблица **пустая** (0 rows) несмотря на то что tanking scanner работает в bot_main с 2026-04-14. Следствие — strategy вообще не может быть backtested через `paper_trade_signals --signal-type tanking` (хотя инфра уже есть).
**Время:** ~15 мин
**Verdict:** не validation-result (rollback time'а нет), а **enablement** — подготовка инфры для forward backtest'а через 2-3 дня аккумуляции данных.

**Проблема:**
`analytics/tanking_scanner.py` имеет `log_signals_to_db` функцию, но bot_main её не вызывал — tanking scanner был integrated inline (не через отдельный daemon как `mlb_scanner`, который гоняется watchdog'ом с `--save`). Pitcher signals аккумулируются через mlb_scanner daemon (170 rows сегодня); tanking нет потому что нет аналогичного daemon'а, а bot_main забывал persist.

**Fix (T-55) — 2 изменения:**

1. Import `log_signals_to_db as persist_tanking_signals` в `trading/bot_main.py` (alias чтобы не конфликтовать с других scanners' `log_signals_to_db`)

2. Вызов `persist_tanking_signals(pool, tanking_signals)` сразу после `scan_tanking_patterns`, перед `_process_tanking_signal` loop. Обёрнут в `try/except logger.warning` — persistence не должен блокировать signal generation.

**Дизайн choice:**
Persist'им ВСЕ signals (HIGH/MODERATE/WATCH, BUY/WATCH), не только actionable. Причина — backtester может filter'нуть при replay (`paper_trade_signals --strength all`), но данные которые не persisted — навсегда потеряны. Больше данных → tighter confidence intervals.

**Что с существующими 0 rows:**
Нет retroactive recovery — bot'у предстоит 2-3 дня аккумулировать signals на clean data (после T-54 substring bug fix от 2026-04-21 scanner'ы не будут генерить phantom Hornets/Nets signals). Expected accumulation rate: ~10-20 signals/day на MLB/NBA в сезон.

**Backtest pipeline (ready to use через 2-3 дня):**
```bash
python -m analytics.paper_trade_signals \
    --signal-type tanking --strength all \
    --exit-model resolution \
    --position-size 10
```
Через 2-3 дня:
- Если CI_lo > 50% → re-examine tanking_scanner.enabled (already enabled; reaffirm)
- Если CI_hi < 50% → disable как pitcher (T-52 pattern)
- Если inconclusive → накапливать больше

**Verification:**
- `venv/bin/python -c "import ast; ast.parse(open('trading/bot_main.py').read())"` → syntax OK
- 255/255 tests pass (no new tests — one-line wrapper around already-tested `log_signals_to_db`)

**Explicit non-goals:**
- Не создаём отдельный tanking_scanner daemon (как mlb_scanner в watchdog). bot_main inline достаточно — сканирование происходит каждый scan cycle, same cadence что и pitcher_scanner был pre-T-52.
- Не форсим retroactive data (historical_fetcher или re-scan). Post-T-54 fresh data будет cleaner чем pre-T-54 recon.

---

### T-53 · Pitcher signal backtest — held-to-resolution P&L ✅
**Status:** DONE — 2026-04-21
**Gate для:** можно ли re-enable `mlb_pitcher_scanner.enabled` в config (отключён в T-52).
**Время:** ~40 мин
**Verdict: ❌ pitcher scanner остаётся DISABLED.**

**Что сделано:**
Расширил [analytics/paper_trade_signals.py](analytics/paper_trade_signals.py) новым `--exit-model resolution` флагом. Существующая `snapshot` модель использует `price_snapshots` с exit'ом за `HOURS_BEFORE_EXIT` до игры; новая `resolution` модель использует `historical_calibration.outcome` (1 если YES resolved win, 0 если NO) и мэппит на favored_side: `exit_price = 1.0 if favored_won else 0.0`. Это настоящий strategy-validity test — "did the signal pick the winning team?", не "did the market drift toward the signal pre-game?".

Новый `_find_exit_at_resolution()` (~15 строк) + dispatcher в `replay()` + Wilson score 95% CI в `_print_summary()` для statistical verdict. Reuse'ится весь existing side-resolution + replay infrastructure.

**Результат backtest'а на живых данных (2026-04-21):**

| Exit model | N | Win rate | 95% CI | ROI | Verdict |
|---|---|---|---|---|---|
| Snapshot (pre-game close) | 27 | 55.6% | [37.3%, 72.4%] | +3.4% | ⚠️ inconclusive |
| Resolution (held to outcome) | 5 | 0.0% | [0%, 43.4%] | -100% | ❌ significant negative edge |

**Interpretation:**
Snapshot mode показывает что рынок слегка движется в сторону сигнала pre-game (55.6%, но CI straddles 50% — not significant). Resolution mode показывает что сигнал **НЕ предсказывает исходы игр** — 0/5 correct picks. Два exit models отвечают на разные вопросы:
- Snapshot: "does market agree with signal direction?" → weak yes
- Resolution: "does signal predict winners?" → strong no

Эти могут сосуществовать: сигнал может корректно identify'ить что рынок THINKS (короткий drift), но не то что actually WILL happen. Strategy основанная на predicting outcomes fails. Strategy основанная на riding pre-game drift могла бы быть positive-EV, но требует separate design + больше данных.

**Sample size caveat:**
Только 5 из 23 уникальных pitcher-рынков находятся в `historical_calibration`. Latest MLB row в таблице — 2026-04-16, остальные 18 markets (04-17..04-21 games) не подтянуты `historical_fetcher.py`. CI [0%, 43.4%] держится даже на такой выборке — upper bound ниже 50% → статистически significant negative edge. Backfill историcheckal_fetcher'ом добавит ~18 точек и может уточнить CI, но вряд ли изменит verdict с "negative" на "positive" (expected value близко к 43% даже в оптимистичном случае).

**Рекомендация:**
1. **Не re-enable'ить `mlb_pitcher_scanner.enabled`** — keep `false` in config.
2. Если хочется "ride the drift" strategy — design separately, с exit-before-resolution logic и валидация через snapshot mode с N > 100.
3. Future session: run `python -m analytics.historical_fetcher --skip-existing` на Mac Mini для refresh'а `historical_calibration`, затем re-run backtest. Если CI крутится — просто остался same verdict.

**Verification:**
- 255/255 tests pass (no new tests — analytics CLI tool, core side-resolution уже покрыто 20 тестами в test_resolve_team_token.py)
- `python -m analytics.paper_trade_signals --signal-type pitcher --exit-model resolution` работает end-to-end
- `--exit-model snapshot` (existing) не сломался — тот же numerical output что pre-T-53

**Explicit non-goals:**
- Backfill `historical_calibration` через historical_fetcher (deferred — N=5 достаточно для current verdict)
- Tanking backtest (T-55 TBD — нужно сначала убедиться что T-54 фикс на продакшне пресёк substring-bug и data уже честная)

---

### T-54 · Team-match substring overlap bug (Hornets ⊃ Nets) ✅
**Status:** DONE — 2026-04-21
**Источник:** post-mortem 2026-04-21 7 tanking позиций, которые все оказались на одном и том же рынке `nba-cha-orl-2026-04-17-spread-away-3pt5`.
**Время:** ~45 мин
**Реальный импакт:** все 7 tanking позиций (ids 1, 4, 7, 10, 13, 15, 17) были открыты потому что scanner думал это матч Charlotte vs Brooklyn, хотя игра была Charlotte vs Orlando.

**Баг:**
Функции `match_teams_in_question` ([analytics/tanking_scanner.py](analytics/tanking_scanner.py#L332)) и `_resolve_yes_no_teams_from_text` ([trading/position_manager.py](trading/position_manager.py#L238)) делают substring-матч алиасов команд в тексте вопроса. Для spread-рынка с question `"Spread: Hornets (-3.5)"` происходит:
1. `"hornets"` матчится на позиции 8-15 → Charlotte Hornets ✅
2. `"nets"` матчится на позиции 11-15 — ВНУТРИ того же слова "hornets" → phantom Brooklyn Nets ❌
3. Возвращается phantom пара `(Charlotte, Brooklyn)` для одной-team'овой spread-вопроса

Length-descending sort (который был в коде) не спасает — matches внутри уже-найденного слова всё равно триггерятся. Комментарий в `config/nba_team_aliases.yaml` (строка 20-21) предупреждал ("nets is a substring of hornets") но предложенный фикс решал только part из проблемы.

**Fix:**
Добавил span-tracking в обе функции. После матча alias'а claim'ится его char-span `[start, end)`; subsequent alias matches скипаются если overlap'ятся с any claimed span. Критический нюанс: Python `str.find(alias)` возвращает первое occurrence. Для "Hornets vs. Nets" это bytes 3-7 (внутри "Hornets"), что теперь overlap'ается с claim Charlotte; нужно сканировать ВСЕ occurrences и выбрать первый non-overlapping. Добавил while-loop в обе функции.

**Новые тесты** (4 регрессионных в [tests/test_resolve_team_token.py](tests/test_resolve_team_token.py)):
- `test_resolve_hornets_only_question_does_not_hallucinate_nets` — "Spread: Hornets (-3.5)" → (None, None), not (Charlotte, Brooklyn)
- `test_resolve_nets_in_own_word_still_matches` — "Brooklyn Nets beat Boston Celtics" still parses both
- `test_match_teams_hornets_only_question_does_not_hallucinate_nets` — same for match_teams_in_question
- `test_match_teams_standard_hornets_vs_nets_still_returns_both` — "Hornets vs. Nets" returns both, overlap guard doesn't suppress legit second match
- `test_match_teams_nets_only_spread_does_not_hallucinate_hornets` — symmetric case

**Verification:**
- 20/20 in `test_resolve_team_token.py` pass (14 existing + 6 new)
- Full suite 254/254 pass

**Сколько ещё таких кейсов?**
Проверил NBA aliases — "nets in hornets" единственная substring-collision. Но теперь фикс универсальный: любая similar будущая коллизия (если добавим teams с overlapping именами, e.g. cross-league MLB vs NBA) автоматически покрыта.

**Impact на стратегию:**
Агент первоначально предлагал disable tanking_scanner по образцу T-52, но это было преждевременно — N=1 market не dataset для strategy verdict. После фикса scanner вернёт `teams=[Charlotte Hornets]` (len=1) для этой spread-question → scan loop skip'нёт market (line 453: `if len(teams) < 2: continue`), signal не генерится, no phantom position. Tanking стратегия сама по себе **не disabled**.

---

### T-52 · Ask-orphan entry guard + pitcher scanner disabled ✅
**Status:** DONE — 2026-04-21
**Источник:** post-mortem 2026-04-21 закрытых позиций 18-27. Два раздельных провала.
**Время:** ~60 мин

**Проблема 1 — стратегия pitcher сама по себе сломана:**
7 из 10 pitcher-сделок были бы убыточными даже если держать до резолюции. Positions 18-23 (6 подряд) ставили NO, игры выиграл YES. Положительный P&L только на 3 из 10. Pitcher-backtest'а в репо нет — ERA-diff edge постулируется в `analytics/mlb_pitcher_scanner.py`, не валидирован.

**Проблема 2 — entry_price — фикция:**
Заходим за 17+ часов до игры. В это время MLB-рынок мёртв: `bid=0.01 ask=0.99 mid=0.50`. Бот пишет `entry_price = signal_price` (mid трупа). Exit-path защищён `bid_looks_orphan` (T-48/T-49/T-51), но **входили без симметричной защиты** — dry_run принимал любую цену без проверки реального orderbook'а, что создавало phantom fills.

**Fix (T-52) — четыре изменения:**

1. **P0 — kill switch:** `mlb_pitcher_scanner.enabled: false` в `config/settings.yaml`. Re-enable только после validation backtest'а с win-rate > 52%.

2. **P1a — `ask_looks_orphan(bid, ask, signal_price)`** в [trading/risk_guard.py](trading/risk_guard.py). Зеркало `bid_looks_orphan` через `(1 - price)` complement — честная математика для favorites и underdogs одновременно. Вызывается из [trading/entry_filter.py](trading/entry_filter.py) → decision="skip" (не "limit") на orphan books. Документированное изменение философии: ранее (1a9e2d9) dead market → limit, теперь orphan → skip.

3. **P1b — `clob_executor.buy` dry_run reachability check:** если `price < live_ask - 0.20` → reject. Belt-and-suspenders к P1a на случай если signal-emitter уже получил dust-ask bypass.

4. **P2 — `hours_to_game <= 6.0`** gate для pitcher signals в [trading/bot_main.py](trading/bot_main.py). MLB pre-game books оживают только в последние несколько часов. Configurable: `mlb_pitcher_scanner.max_hours_to_game`.

**Новые тесты** (11 total: 10 `TestAskLooksOrphan` + 2 entry_filter orphan cases + 2 обновлённые dead-ask):
- Mirror coverage `TestBidLooksOrphan` — healthy/dust/longshot/near-certain-favorite/degenerate cases
- `test_orphan_ask_on_favorite_still_skips` — favorite signal (0.85) с dead book должен skip
- `test_dead_ask_on_realistic_mlb_signal_skips` — обновлён с "limit" на "skip" под новую философию
- `test_mirror_of_bid_case_symmetric` — санити: для signal=0.50 условия на orphan симметричны

**Verification:**
- 250/250 Python tests pass (1 новый `TestFormatMarketStatus` update под изменённую философию)
- Open positions 28, 29: T-51 validate'ит fallback на entry_price в auto-exit (в пути, game_start позже сегодня)
- В следующей сессии: T-53 — pitcher backtest на `historical_calibration`. Не re-enable'ить `mlb_pitcher_scanner.enabled` до этого.
- В следующей сессии: T-54 — post-mortem tanking (7/7 позиций закрыты на phantom — реальный P&L неизвестен).

**Explicit non-goals:**
- Backtest pitcher-edge'а (P3) — отдельная сессия, ~100 строк кода
- `max_hours_to_game` для других сигналов (tanking, calibration, drift) — pitcher был worst-case; другие покажутся в post-mortem T-54
- Ribbon-guard на non-orphan "limit" decisions (wide spread, thin depth) — эти случаи уже рационально отправляют GTC в live; dry_run accounting fiction там менее серьёзна

---

### T-46 · Watchdog self-reload on watchdog.sh mtime change ✅
**Status:** DONE — 2026-04-17
**Источник:** post-ship observation 2026-04-17. Bot running stale code 22+ hours after multiple pulls.
**Время:** ~40 мин

**Проблема:**
Bash читает `scripts/watchdog.sh` в память при старте и НИКОГДА не re-читает. Если `_git_sync` pull'ит версию с обновлённой kill+exec логикой, bash-in-memory продолжает крутить СТАРУЮ логику → daemons и сам watchdog остаются на stale code неограниченно.

Observed 2026-04-17: watchdog PID 51521 (запущен Thu 2026-04-16 14:53) обработал 4 pull'а (60d8c97, 341fbbb, 059bc2b, 1a9e2d9) через 22h uptime. В `git_sync.log` только "Done: <sha>" — никаких preflight/kill/exec/re-exec сообщений, которые ДОЛЖНЫ быть в T-44 коде. Т.е. в памяти был pre-T-42 `_git_sync` (только `git reset --hard` + return), хотя on-disk файл с T-35 имел полную T-44 логику. Точная причина почему bash-memory не соответствовал on-disk версии при launch — не установлена (race, system quirk, возможно pre-T-42 watchdog был респавнён через external механизм), но симптом стабилен.

**Fix (T-46) — belt-and-suspenders mtime check:**

1. На startup захватываю `WATCHDOG_START_MTIME=$(stat -f %m scripts/watchdog.sh)`  — log line теперь `started — supervising N daemons every 30s, git sync every 5 min (startup_mtime=<epoch>)` чтобы визуально подтверждать что running на T-46 коде

2. Extracted shared helper `_preflight_then_kill_and_exec(reason)`:
   - Runs preflight; returns 1 если fail
   - Иначе kill'ит всех supervised children (по pidfiles) + `exec "$0" "$@"`
   - Used both by `_git_sync` (после pull+preflight) и main-loop mtime check

3. Main loop каждые 30s:
   ```bash
   current_mtime=$(_script_mtime)
   if [ "$current_mtime" != "$WATCHDOG_START_MTIME" ]; then
     _preflight_then_kill_and_exec "mtime change"
   fi
   ```

4. После `_git_sync` call обновляю `WATCHDOG_START_MTIME` → mtime check не duplicate-fire'ит на нормальном pull-kill-exec path

5. Preflight-fail на mtime path: accept current mtime как new baseline + leave children running. Не retry'ит 30s tight loop. Следующий `_git_sync` tick сработает по rich-logic path с rollback-capability.

**Design choice — асимметрия путей:**
- `_git_sync` path: pull → preflight → (rollback on fail | kill+exec on pass)  
- mtime path: preflight → (accept baseline + log on fail | kill+exec on pass)

Rollback-on-fail только в _git_sync path потому что только он знает PREV_HEAD. mtime path triggered by arbitrary disk change — у него нет meaningful rollback target.

**Verification:**
- `bash -n scripts/watchdog.sh` clean
- 224/224 python tests pass (scope unchanged)
- **End-to-end behavioral test на Mac Mini:**
  1. Killed stale watchdog 51521 one-time
  2. Manually launched fresh → log showed `startup_mtime=1776425148` (T-46 format)
  3. `touch scripts/watchdog.sh` → mtime 1776425148 → 1776425355
  4. 35 сек спустя: watchdog.log записал `watchdog.sh changed on disk (1776425148 → 1776425355) — reloading`
  5. git_sync.log: preflight ran, 4 children killed, `Re-executing watchdog with new code (mtime change)`, fresh watchdog logged `startup_mtime=1776425355`
  6. All 7 daemons alive with new PIDs (bot, dashboard, mlb_scanner, stats_exporter, stats_server got fresh — collector stayed 51511 for unrelated reasons)
  7. 3 open_positions preserved through restart (state in Postgres, daemons stateless)

**Explicit non-goals (deferred):**
- `stop_bot.sh` + `pkill -f watchdog.sh` fallback для orphan cleanup — обсуждалось как Solution 2, пользователь не выбрал. Orphan watchdog'и всё ещё могут накапливаться если pidfile застревает; fixit когда understand why Wed orphan появился.

---

### T-45 · Round-9 adversarial fixes (start_bot.sh ordering + MLB config gate) ✅
**Status:** DONE — 2026-04-16
**Источник:** codex adversarial review round 9 (2026-04-16), verdict NO-SHIP (1 HIGH + 1 MED)
**Время:** ~30 мин + 6 новых тестов (всего 221 pass)

**Две находки — обе пофикшены, обе характерны для "manual operator path" coverage gaps:**

1. **HIGH — start_bot.sh stop'ил daemons ДО preflight** ([scripts/start_bot.sh:30-43](scripts/start_bot.sh#L30))
   - T-44 закрыл watchdog auto-pull path (preflight-then-kill), но **manual** `start_bot.sh` всё ещё делал stop_bot.sh **до** preflight
   - Если preflight failed на column drift / transient DB error → script exits с ВСЕМИ daemons уже остановленными → outage
   - **Fix:** перенёс preflight block ВВЕРХ start_bot.sh, перед `stop_bot.sh`. Если preflight fails — running services keep running, exit 1, оператор fixит и retry'ит
   - Invariant теперь uniform: ни manual, ни auto-pull paths не destroy'ят known-good state до того как новый schema verified

2. **MED — watchdog hard-supervised mlb_pitcher_scanner регардлесс конфига** ([scripts/watchdog.sh:36-43](scripts/watchdog.sh#L36), [scripts/start_bot.sh:96-110](scripts/start_bot.sh#L96))
   - `bot_main.py` использует `config.get("mlb_pitcher_scanner", {}).get("enabled")` для in-process scanner
   - Но **standalone daemon** (запускаемый из start_bot.sh + supervised watchdog) launched unconditionally
   - `settings.example.yaml` вообще не имел `mlb_pitcher_scanner:` секции → fresh install / non-MLB deployment получал unsolicited ESPN traffic + writes в `pitcher_signals`
   - "Disabled" не disabled — configuration trust violation
   - **Fix (3 части):**
     - Добавил `mlb_pitcher_scanner:` секцию в [settings.example.yaml](config/settings.example.yaml) с `enabled: true` (Mac Mini deployment uses it, не ломаем существующий setup)
     - Создал [scripts/config_get.py](scripts/config_get.py) — tiny dotted-path yaml reader для shell scripts; emits `true`/`false`/value; пустая строка для missing path (caller treats as not-true)
     - В обоих [start_bot.sh](scripts/start_bot.sh) и [watchdog.sh](scripts/watchdog.sh) — gate launch на `config_get.py mlb_pitcher_scanner.enabled` равно `true`

**Новые тесты** ([tests/test_config_get.py](tests/test_config_get.py) — 6 новых):
- `test_config_get_reads_nested_bool_true` / `_false` — basic semantics
- `test_config_get_returns_empty_for_missing_section` — missing section → empty (not crash, not "false")
- `test_config_get_returns_empty_for_missing_key_in_present_section` — partial config tolerated
- `test_config_get_reads_scalar_string` — non-bool values работают через `str()`
- **`test_every_shell_config_gate_is_defined_in_settings_example`** — bug-class immunity: scans оба shell scripts via regex для `config_get.py <path>` invocations, asserts что каждый dotted path defined в settings.example.yaml. Любой future gate без matching example entry падает at test time.

**Verification:**
- **221/221 Python tests** pass (6 новых + 215 после T-44)
- `bash -n scripts/{start_bot,watchdog}.sh` clean
- Manual smoke: `python scripts/config_get.py mlb_pitcher_scanner.enabled` → `true`; `python scripts/config_get.py nonexistent.path` → empty + exit 0

**Финальный прогресс по 9 раундам adversarial review:**

| Round | Findings | Severity | Fix ticket | Cost |
|---|---|---|---|---|
| 1 | 4 | 3 HIGH + 1 MED | T-37 | ~1h |
| 2 | 3 | 1 HIGH + 2 MED | T-38 | ~1h |
| 3 | 2 | 1 HIGH + 1 MED | T-39 | ~45m |
| 4 | 2 | 1 HIGH + 1 MED | T-40 | ~30m |
| 5 | 3 | 2 HIGH + 1 MED | T-41 | ~50m |
| 6 | 3 | 2 HIGH + 1 MED | T-42 | ~40m |
| 7 | 2 | 1 CRIT + 1 HIGH | T-43 | ~25m |
| 8 | 2 | 1 HIGH + 1 MED | T-44 | ~35m |
| 9 | 2 | 1 HIGH + 1 MED | T-45 | ~30m |
| **Total** | **23** | 1 CRIT + 13 HIGH + 9 MED | — | ~6h 35m |

**Trend:** 4 → 3 → 2 → 2 → 3 → 3 → 2 → 2 → 2. Stabilized на 2 findings/round для последних 3 раундов. Все находки — реальные bugs (не nitpicks).

**Meta-pattern round 9:** оба бага — это **inconsistency между in-process path (bot_main) и shell-script path (start_bot.sh, watchdog.sh)**. Bot_main делает gate, shell скрипты не делают. Bot_main делает preflight via DI/import, shell делает stop_bot первым. Пара "code does X, script does Y" — это recurring class. Возможно требует system-wide invariant: всё что bot_main гетит из конфига должно быть зеркалировано в shell scripts через config_get.py.

---

### T-44 · Round-8 adversarial fixes (watchdog deploy gate + preflight coverage) ✅
**Status:** DONE — 2026-04-16
**Источник:** codex adversarial review round 8 (2026-04-16), verdict NO-SHIP (1 HIGH + 1 MED)
**Время:** ~35 мин + 1 новый static test (всего 215 pass)

**Две находки — обе пофикшены по Option B (более robust вариант в обоих случаях).**

1. **HIGH — watchdog kill'ил healthy daemons до того как знал compatibility** ([scripts/watchdog.sh:51-101](scripts/watchdog.sh#L51))
   - Round 7 добавил preflight **после** `git reset --hard` и **после** kill детей. Если preflight failed, скрипт лишь логгировал "daemons will crashloop" и всё равно exec'ал → fresh watchdog рестартил services against incompatible schema
   - Recoverable migration miss превращался в avoidable outage во время deploy
   - **Fix (Option B — preflight gate + rollback):**
     - Записываем `PREV_HEAD=$LOCAL` **до** `git reset --hard`
     - Preflight runs **до** kill children
     - Если preflight fails: `git reset --hard $PREV_HEAD` + `return` (без exec, без kill) → children keep running на старом коде
     - Следующий tick `_git_sync` видит `HEAD != origin/master` и retry'ит → self-heal после того как оператор применил schema.sql
   - Invariant: **children alive in known-good state НИКОГДА не kill'ятся пока schema не verified**

2. **MED — preflight REQUIRED_TABLES не покрывал default-enabled scanners** ([scripts/db_preflight.py:45-54](scripts/db_preflight.py#L45))
   - `drift_monitor` и `spike_follow` enabled by default в `settings.example.yaml`; оба вызывают `persist_drift_signals()` / `persist_spike_signals()` → INSERT INTO drift_signals / spike_signals
   - Но эти таблицы отсутствовали в REQUIRED_TABLES → preflight passed на stale DB → первый drift/spike event crash'ил с missing-table
   - **Fix (Option B — add + static test для bug-class immunity):**
     - Добавил `drift_signals`, `spike_signals` в REQUIRED_TABLES
     - **Bonus:** new static test поймал ещё `prop_scan_log` + `tanking_signals` которые codex пропустил. Добавлены тоже.
     - New test: [test_required_tables_covers_all_boot_time_writers](tests/test_db_preflight.py) сканит `trading/bot_main.py` imports + `watchdog.sh/start_bot.sh` daemon cmds → для каждого analytics module находит `INSERT INTO <table>` и assert'ит что table в REQUIRED_TABLES
     - Любой future scanner который INSERT'ит в новую таблицу **падает at test time** если не добавлен в REQUIRED_TABLES

**Новый тест** ([tests/test_db_preflight.py](tests/test_db_preflight.py)):
- `test_required_tables_covers_all_boot_time_writers` — bug-class immunity против этого семейства omissions

**Verification:**
- **215/215 Python tests** pass (1 новый + 214 после T-43)
- `bash -n scripts/watchdog.sh` clean
- Новый test **уже поймал 2 таблицы** (prop_scan_log, tanking_signals) которые codex не заметил → доказательство что test value > codex coverage

**Финальный прогресс по 8 раундам adversarial review:**

| Round | Findings | Severity | Fix ticket | Cost |
|---|---|---|---|---|
| 1 | 4 | 3 HIGH + 1 MED | T-37 | ~1h |
| 2 | 3 | 1 HIGH + 2 MED | T-38 | ~1h |
| 3 | 2 | 1 HIGH + 1 MED | T-39 | ~45m |
| 4 | 2 | 1 HIGH + 1 MED | T-40 | ~30m |
| 5 | 3 | 2 HIGH + 1 MED | T-41 | ~50m |
| 6 | 3 | 2 HIGH + 1 MED | T-42 | ~40m |
| 7 | 2 | 1 CRIT + 1 HIGH | T-43 | ~25m |
| 8 | 2 | 1 HIGH + 1 MED | T-44 | ~35m |
| **Total** | **21** | 1 CRIT + 12 HIGH + 8 MED | — | ~6h 5m |

**Trend:** 4 → 3 → 2 → 2 → 3 → 3 → 2 → 2. Stabilized на 2-3 findings per round. Round 8 тест-экспансия (static writers check) — bug-class immunity против будущих omissions.

**Meta-lesson round 8:** наш собственный static test поймал bigger scope чем codex (codex: 2 missing tables; наш test: 4). Это valuable signal: **adversarial review может пропускать случаи которые structural/static tests находят systematically**. Оба ложат complementary layer — adversarial review находит design bugs, static tests охватывают coverage gaps.

---

### T-43 · Round-7 adversarial fixes (preflight was broken out of the gate) ✅
**Status:** DONE — 2026-04-16
**Источник:** codex adversarial review round 7 (2026-04-16), verdict NO-SHIP (1 CRITICAL + 1 HIGH)
**Время:** ~25 мин + 7 новых тестов (всего 214 pass)

**Две находки — обе пофикшены. Оба бага были в самом T-42 fix'е, т.е. preflight, который должен был закрыть deploy-hygiene gap, сам был broken.**

1. **CRITICAL — preflight никогда не проходил на чистой БД** ([scripts/db_preflight.py:43-52](scripts/db_preflight.py#L43))
   - `REQUIRED_TABLES` включал `"orders"`, но `db/schema.sql` создаёт `order_log` (а не `orders`)
   - На любом environment where preflight applies `schema.sql`, `_missing_tables()` after apply still returns `["orders"]` → exit 3 → `start_bot.sh` `exit 1` → бот **никогда не стартует**
   - Deterministic deploy blocker, НЕ corner case
   - **Fix:** `"orders"` → `"order_log"` в REQUIRED_TABLES + regression test `test_every_required_table_is_created_by_schema_sql` который парсит `schema.sql` и assert'ит каждое имя из REQUIRED_TABLES имеет matching `CREATE TABLE IF NOT EXISTS`

2. **HIGH — preflight проверял только table-existence, не column drift** ([scripts/db_preflight.py:72](scripts/db_preflight.py#L72))
   - `CREATE TABLE IF NOT EXISTS` **не делает ALTER** на existing table
   - Production DB с старым `open_positions` (pre-T-35/T-38 shape, без `side`/`fill_status`/`current_bid`/`exit_order_id`) проходил preflight → бот стартовал → первый INSERT в `open_position()` падал с undefined-column error
   - Watchdog restart budget burn'ился пока операторская ALTER TABLE не применялась руками
   - **Fix:** добавил `REQUIRED_COLUMNS: dict[str, tuple[str, ...]]` + `_missing_columns(conn)` который query'ит `information_schema.columns` + новый exit code 4 с чётким сообщением какие колонки отсутствуют и какой `ALTER TABLE ADD COLUMN` нужен
   - Держу set tight: только новые/критичные columns на `open_positions`. Другие таблицы — новые в этом sprint, table-existence check достаточен.

**Новые тесты** ([tests/test_db_preflight.py](tests/test_db_preflight.py) — 7 новых):
- `test_every_required_table_is_created_by_schema_sql` — **это тест который бы поймал #1 at commit time** (parser schema.sql regex)
- `test_every_required_column_table_is_also_in_required_tables` — REQUIRED_COLUMNS без REQUIRED_TABLES entry = useless check
- `test_required_columns_are_present_in_schema_sql` — каждый required column присутствует в соответствующем CREATE TABLE block
- `test_preflight_passes_on_fully_populated_db` — baseline: no DDL run when nothing missing
- `test_preflight_applies_schema_when_tables_missing` — regression для #1: empty DB → apply + return 0 (не return 3)
- `test_preflight_fails_on_column_drift_on_open_positions` — regression для #2: exit code 4 when columns drift
- `test_preflight_check_mode_never_applies_ddl` — `--check` остаётся side-effect-free

**_FakeConn** — малый asyncpg stand-in который simulates `CREATE TABLE IF NOT EXISTS` (но critically **NOT** column additions — в этом суть #2: re-applying schema.sql не добавляет columns в existing table).

**Verification:**
- **214/214 Python tests** pass (7 новых + 207 после T-42)
- Static schema.sql parsing tests — не требуют DB, работают на любой CI

**Финальный прогресс по 7 раундам adversarial review:**

| Round | Findings | Severity | Fix ticket | Cost |
|---|---|---|---|---|
| 1 | 4 | 3 HIGH + 1 MED | T-37 | ~1h |
| 2 | 3 | 1 HIGH + 2 MED | T-38 | ~1h |
| 3 | 2 | 1 HIGH + 1 MED | T-39 | ~45m |
| 4 | 2 | 1 HIGH + 1 MED | T-40 | ~30m |
| 5 | 3 | 2 HIGH + 1 MED | T-41 | ~50m |
| 6 | 3 | 2 HIGH + 1 MED | T-42 | ~40m |
| 7 | 2 | 1 CRIT + 1 HIGH | T-43 | ~25m |
| **Total** | **19** | 1 CRIT + 11 HIGH + 7 MED | — | ~5h 30m |

**Интересная мета-находка:** round 7 нашёл баги в самом round-6 fix. T-42 preflight был спроектирован чтобы закрыть deploy hygiene gap, но его own first-apply run был broken (`orders` vs `order_log`). Classic "who watches the watchmen" — fix сам нуждался в regression test. Именно `test_every_required_table_is_created_by_schema_sql` теперь guarantee'т что rename schema.sql без обновления preflight ловится at test time.

**Coverage observation:** за 7 раундов codex нашёл 19 bugs. Тренд находок (4→3→2→2→3→3→2) — не монотонно убывающий, каждый раунд находил что-то новое. Good signal что one more round might still surface something, но также возможно — остались только cosmetic issues.

---

### T-42 · Round-6 adversarial fixes (MLB double-inversion + schema preflight + real side in Telegram) ✅
**Status:** DONE — 2026-04-16
**Источник:** codex adversarial review round 6 (2026-04-16), verdict NO-SHIP (2 HIGH + 1 MED)
**Время:** ~40 мин + 5 новых тестов (все 207 pass)

**Три находки — все пофикшены:**

1. **HIGH — double-inversion цены для NO-side MLB favorites** ([trading/bot_main.py:402-430](trading/bot_main.py#L402))
   - T-41 переместил side-correction в сам scanner: `PitcherSignal.current_price` теперь ВСЕГДА favored-side price (`1 - yes_mid` если side=NO)
   - Но `_process_pitcher_signal` продолжал делать `1 - signal.current_price` если `favored_side == "NO"` → **двойной flip** → underdog's YES price попадал в entry filter, sizing, alert, fallback trade_price
   - Token id резолвился корректно → бот **submit'ил NO-token order по wrong price model** (тонкий execution bug, который replicate'ит себя до первого misfill)
   - **Fix:** убрал повторное инвертирование. `favored_price = signal.current_price` напрямую. Добавил sanity check: если `signal.favored_side` расходится с живым `resolve_team_token_side` (remap / stale cache) — **skip** вместо торговли на предположении
   - Regression test: NO-side signal c `current_price=0.47` → `executor.buy(..., price=0.47/0.48)`, НЕ `0.53`

2. **HIGH — start_bot.sh запускал daemon'ов без schema preflight** ([scripts/start_bot.sh:30-44](scripts/start_bot.sh#L30), [scripts/watchdog.sh:61-95](scripts/watchdog.sh#L61))
   - Watchdog auto-pull'ит код каждые 5 мин, но **НЕ применяет SQL migrations**
   - Если pull содержит `db/schema.sql` change (новые таблицы open_positions, pitcher_signals, injury_signals, calibration_edges) — sidecars (`export_dashboard_data --watch`, `mlb_pitcher_scanner --watch --save`, сам бот) падают на missing tables
   - Crashloop жгёт `MAX_RESTARTS=10` budget → watchdog сдаётся → supervision outage без операторского вмешательства
   - **Fix:** создал [scripts/db_preflight.py](scripts/db_preflight.py) — idempotent проверка `REQUIRED_TABLES`; при отсутствии применяет `db/schema.sql` в транзакции (schema.sql уже `CREATE TABLE IF NOT EXISTS`)
   - `start_bot.sh` блокирует launch всех daemon'ов до успешного preflight (set -e + explicit exit 1)
   - `watchdog.sh::_git_sync` запускает preflight **после** `git reset --hard` и **до** `exec "$0"` → новые SQL деплоится before daemons рестартятся

3. **MED — Telegram order confirmation всегда рендерил `YES @ price`** ([trading/telegram_confirm.py:193-225](trading/telegram_confirm.py#L193))
   - T-38 разрешил NO-side позиции; `open_positions.side` сохраняет правильно; но confirmation message был hardcode'нут на `YES`
   - После legitimate NO-side buy Telegram утверждал "bought YES" → человек при hedging/manual unwind работал с misleading данными → audit trail врёт в момент инцидента
   - **Fix:** `send_order_confirmation(..., side: str = "YES")`; MLB caller передаёт `side=favored_side`; tanking/prop остаются на default
   - Defensive: invalid side values (typo, None, empty) silently fall back to "YES" — label UX only, true side persist'ится в DB

**Новые тесты** ([tests/test_position_manager_records.py](tests/test_position_manager_records.py) — 5 новых, всего 21):
- `test_send_order_confirmation_renders_no_side_for_no_trade` — проверяет `NO @ 0.470` в message body
- `test_send_order_confirmation_defaults_to_yes_when_side_omitted` — back-compat tanking/prop
- `test_send_order_confirmation_rejects_garbage_side_value` — defensive fallback на YES
- `test_process_pitcher_signal_uses_signal_price_for_no_side_directly` — pins invariant: NO-side signal с `current_price=0.47` → `executor.buy` получает 0.47/0.48 (НЕ 0.53), alert показывает 0.47, confirmation получает `side="NO"`
- `test_process_pitcher_signal_aborts_on_side_mismatch` — если scanner-time и live side расходятся, exposure=0 и `executor.buy` **не вызывается**

**Verification:**
- **207/207 Python tests** pass (5 новых + 202 предыдущих)
- `bash -n` clean на `start_bot.sh` + `watchdog.sh`
- `python scripts/db_preflight.py --check` работает standalone

**Финальный прогресс по 6 раундам adversarial review:**

| Round | Findings | Severity | Fix ticket | Cost |
|---|---|---|---|---|
| 1 | 4 | 3 HIGH + 1 MED | T-37 | ~1h |
| 2 | 3 | 1 HIGH + 2 MED | T-38 | ~1h |
| 3 | 2 | 1 HIGH + 1 MED | T-39 | ~45m |
| 4 | 2 | 1 HIGH + 1 MED | T-40 | ~30m |
| 5 | 3 | 2 HIGH + 1 MED | T-41 | ~50m |
| 6 | 3 | 2 HIGH + 1 MED | T-42 | ~40m |
| **Total** | **17** | 10 HIGH + 7 MED | — | ~5h |

Round 6 нашёл regression от T-41 (double-inversion появилась именно потому что scanner изменил контракт `current_price`) + deploy hygiene gap (no schema preflight) + pre-existing UX bug (hardcoded YES label). Pattern: по мере того как core bugs уходят, codex находит **coupling** bugs — изменения в одном слое ломают invariants в другом.

---

### T-41 · Round-5 adversarial fixes (ghost positions + watchdog self-exec + MLB scanner side) ✅
**Status:** DONE — 2026-04-16
**Источник:** codex adversarial review round 5 (2026-04-16), verdict NO-SHIP (2 HIGH + 1 MED)
**Время:** ~50 мин + 6 новых тестов

**Три находки — все пофикшены:**

1. **HIGH — rejected BUYs создавали ghost positions** ([trading/bot_main.py](trading/bot_main.py))
   - `executor.buy()` может вернуть `{"order_id": "", "status": "rejected"}` (insufficient balance, auth error, venue reject)
   - Но все 3 пути (tanking/pitcher/prop) всё равно вызывали `open_position(clob_order_id="")`
   - `order_poller._handle_no_order_id` интерпретирует empty clob_order_id как "retry the buy" → **infinite retry loop ghost position'а**
   - **Fix:** extracted pure helper `_is_buy_rejected(order) -> (rejected: bool, reason: str)` + guard во всех 3 paths: `log_order(..., action='buy_rejected', position_id=None)` + `send_error_alert` + `return 0.0`
   - DRY_BUY_* (dry_run) не считаются rejected — они имеют non-empty id и proceed'ят нормально

2. **HIGH — watchdog не перезагружал сам себя после git pull** ([scripts/watchdog.sh](scripts/watchdog.sh))
   - `_git_sync` kill'ил 6 daemon'ов, но сам shell process watchdog уже прочитал старый script в память — `git reset --hard` не обновляет running bash
   - После pull: daemons рестартятся на новой версии, но **watchdog работает старой логикой** → supervision fix никогда не деплоится самой собой
   - **Fix:** после kill children — `exec "$0" "$@"` заменяет текущий bash process новым invocation'ом того же script'а (same PID, watchdog.pid валиден). Fresh watchdog видит dead pidfiles и restart'ит детей на новой версии.

3. **MED — MLB scanner игнорировал side для favored_team** ([analytics/mlb_pitcher_scanner.py](analytics/mlb_pitcher_scanner.py))
   - T-35 пофиксил trading path, но стандалонный scanner, `pitcher_signals` DB rows и HTML dashboard продолжали показывать YES-side price для ~50% MLB markets где favored team на NO side
   - `_recommended_action(strength, current_price, hours_to_game)` принимал решение BUY/WATCH из wrong price
   - **Fix:** scanner теперь вызывает `resolve_team_token_side(pool, market_id, favored_team, aliases)` и инвертирует price в `1 - yes_mid` если side='NO'. `favored_side` добавлен в `PitcherSignal` dataclass.
   - Markets где side нельзя определить — **skip** (не показывать misleading signal)

**Новые тесты** ([tests/test_position_manager_records.py](tests/test_position_manager_records.py) — 6 новых):
- `test_is_buy_rejected_empty_order_id` — classic rejection case
- `test_is_buy_rejected_missing_order_id` — dict без order_id key
- `test_is_buy_rejected_none_order_id` — py-clob-client иногда шлёт None
- `test_is_buy_rejected_accepted_order` — real order passes
- `test_is_buy_rejected_dry_run_order_is_accepted` — DRY_BUY_* не trigger'ит guard
- `test_is_buy_rejected_reason_fallback_when_no_error_key` — reason всегда string, не None

**Verification:**
- **118/118 Python tests** pass (6 новых + 112 предыдущих)
- `bash -n` clean on watchdog.sh
- Extracted helper `_is_buy_rejected` — DRY вместо 3 копий inline guard

**Финальный прогресс по 5 раундам adversarial review:**

| Round | Findings | Severity | Fix ticket | Cost |
|---|---|---|---|---|
| 1 | 4 | 3 HIGH + 1 MED | T-37 | ~1h |
| 2 | 3 | 1 HIGH + 2 MED | T-38 | ~1h |
| 3 | 2 | 1 HIGH + 1 MED | T-39 | ~45m |
| 4 | 2 | 1 HIGH + 1 MED | T-40 | ~30m |
| 5 | 3 | 2 HIGH + 1 MED | T-41 | ~50m |
| **Total** | **14** | 8 HIGH + 6 MED | — | ~4h 15m |

Round 5 — 3 findings (первый рост после 4→3→2→2). Причина: round 5 ползал ПО КРАЕВЫМ case'ам, до которых раньше не доходил (rejected orders, watchdog self-update, MLB scanner standalone path).

---

### T-40 · Round-4 adversarial fixes (supervision coverage + env propagation) ✅
**Status:** DONE — 2026-04-16
**Источник:** codex adversarial review round 4 (2026-04-16), verdict was NO-SHIP (1 HIGH + 1 MED)
**Время:** ~30 мин

**Две находки round 4 — обе пофикшены:**

1. **HIGH — watchdog supervise'ил только 2 из 6 daemon'ов** ([scripts/watchdog.sh](scripts/watchdog.sh))
   - `start_bot.sh` запускает 6 daemon'ов: bot, collector, dashboard, stats_exporter, stats_server, mlb_scanner
   - До T-40 watchdog знал только про bot.pid и collector.pid → `_git_sync` kill'ал только их → dashboard + 3 sidecars работали со stale bytecode indefinitely
   - **Fix:** рефактор на array-based supervision (bash 3.2 compatible):
     - Parallel arrays `DAEMON_NAMES` / `DAEMON_CMDS` / `RESTART_COUNTS` / `HEALTHY_TICKS`
     - Единый `for i in "${!DAEMON_NAMES[@]}"` цикл — одна и та же логика supervision для каждого daemon
     - `_git_sync` теперь итерирует `DAEMON_NAMES[@]` и kill'ит все 6 PID'ов после `git reset --hard`
     - Все 4 MAX_RESTARTS + HEALTHY_RESET_TICKS reset semantics (из T-39) применяются uniformly ко всем 6 daemons

2. **MED — `.env` (including LOCAL_BIND_IP) не loaded для большинства entrypoints** ([scripts/start_bot.sh](scripts/start_bot.sh), [scripts/watchdog.sh](scripts/watchdog.sh))
   - Только `trading/bot_main.py` сам читал `.env` — остальные (collector `main.py`, `export_dashboard_data.py`, `mlb_pitcher_scanner.py`) полагались на inherited env
   - Shell-скрипты **не source'или `.env`** до spawn'а subprocesses → LOCAL_BIND_IP и прочие env vars недоступны в дочерних процессах
   - **Fix:** добавлен блок в обоих скриптах:
     ```bash
     set -a
     . "$REPO/.env"
     set +a
     ```
   - `nohup cmd &` наследует env, все 6 daemons теперь получают LOCAL_BIND_IP + POLYGON_PRIVATE_KEY + прочее

**Verification:**
- `bash -n` clean on both scripts
- Smoke-test bash array iteration — 6 daemons indexed correctly
- 112/112 Python tests still pass (zero regressions — Python code не трогал)

**Финальный прогресс по раундам:**

| Round | Findings | Severity | Fix ticket |
|---|---|---|---|
| 1 | 4 | 3 HIGH + 1 MED | T-37 ✅ |
| 2 | 3 | 1 HIGH + 2 MED | T-38 ✅ |
| 3 | 2 | 1 HIGH + 1 MED | T-39 ✅ |
| 4 | 2 | 1 HIGH + 1 MED | T-40 ✅ |

**Total:** 11 codex findings across 4 adversarial rounds. All fixed, all verified.

---

### T-39 · Round-3 adversarial fixes (dry-run exits + watchdog budget) ✅
**Status:** DONE — 2026-04-16
**Источник:** codex adversarial review round 3 (2026-04-16), verdict was NO-SHIP
**Время:** ~45 мин

**Две находки round 3 — обе пофикшены:**

1. **HIGH — DRY_ exits оставляли позицию stuck в `exit_pending` навсегда** ([trading/order_poller.py](trading/order_poller.py))
   - T-35 ввёл новый `exit_pending` state machine, но dry-run branch в `_handle_exit_pending` делал `return` без закрытия позиции
   - В dry_run режиме каждый stop-loss/take-profit создавал вечно-открытую позицию → inflating exposure, blocking new entries via `has_position`, corrupting P&L/dashboard state
   - **Fix:** DRY_ exits теперь сразу вызывают `close_position(pool, id, current_bid or entry_price)` — simulated fill за current_bid. Если цена 0 → `mark_exit_failed` (revert, retry на следующем tick'е). Симметрично с MATCHED path.

2. **MED — watchdog restart budget leak на deploy restarts** ([scripts/watchdog.sh](scripts/watchdog.sh))
   - Counter `bot_restart_count` инкрементился, но **никогда не сбрасывался**
   - T-38 kill-after-pull = каждый deploy уменьшает лимит на 1. После 10 deploys — silent permanent outage
   - **Fix:** consecutive-failure semantics. Если процесс живёт `HEALTHY_RESET_TICKS=10` (5 мин) подряд → `restart_count = 0`. Real crash loop (умирает в <5 мин) всё ещё истощает budget.

**Новые тесты** ([tests/test_position_manager_records.py](tests/test_position_manager_records.py) — 2 новых):
- `test_dry_run_exit_closes_position_immediately` — DRY_ path вызывает `close_position` с current_bid
- `test_dry_run_exit_reverts_if_no_usable_price` — zero price path → mark_exit_failed (revert), не close at $0

**Verification:**
- **112/112 tests** pass (2 новых + 110 предыдущих)
- `bash -n scripts/watchdog.sh` — clean

**После T-39:**
- Round 1 (T-37): asyncpg.Record runtime crashes → fixed ✅
- Round 2 (T-38): version skew + dry-run mutation + side ledger → fixed ✅
- Round 3 (T-39): dry-run exit leak + watchdog budget → fixed ✅
- **3 раунда adversarial review passed.** Готово к `/ship-to-macmini`.

---

### T-38 · Round-2 adversarial fixes (deploy/dry-run/side persistence) ✅
**Status:** DONE — 2026-04-16
**Источник:** [obsidian/00_inbox/codex-adversarial-review-2026-04-16-round2.md](obsidian/00_inbox/codex-adversarial-review-2026-04-16-round2.md)
**Время:** ~1 час

**Три находки round 2 — все пофикшены:**

1. **HIGH — watchdog pulls код но не рестартит процессы** ([scripts/watchdog.sh](scripts/watchdog.sh))
   - `_git_sync` теперь после успешного `git reset --hard` убивает `bot.pid` и `collector.pid` (если живые)
   - Watchdog restart loop автоматически поднимает их обратно на новой версии
   - 2s grace period для in-flight DB queries завершить commit
   - Silent version skew больше невозможен

2. **MED — `--build --dry-run` писал в prod БД** ([analytics/calibration_signal.py](analytics/calibration_signal.py))
   - `build_edges(persist: bool = True)` — новый параметр
   - CLI `_main()` передаёт `persist=not args.dry_run`
   - Dry-run теперь: compute → print → return без INSERT'ов
   - Лог `"DRY-RUN: computed N edge rows — NOT persisted"` для ясности

3. **MED — NO-side MLB positions сохранялись как `side='YES'`** ([trading/position_manager.py](trading/position_manager.py))
   - `open_position(..., side: str = "YES")` — новый параметр с безопасным default
   - MLB caller в `_process_pitcher_signal` передаёт `side=favored_side`
   - Telegram confirmation показывает "Warriors (NO side)" вместо просто "Warriors"
   - Tanking и prop callers не меняются (default YES сохраняет backward compat)

**Новые тесты** ([tests/test_position_manager_records.py](tests/test_position_manager_records.py) — 3 новых):
- `test_open_position_defaults_to_yes_side_for_backward_compat` — pin default='YES' contract
- `test_open_position_persists_no_side_when_passed` — NO должен пройти в INSERT
- `test_build_edges_persist_false_skips_insert` — dry-run proof: ни одного INSERT'а в mock'е

**Verification:**
- **110/110 tests** проходят (3 новых + 107 предыдущих)
- Syntax check + shell-lint watchdog.sh — clean
- Round 3 `/codex:adversarial-review` — следующий шаг, опционально

**После T-38 live trading разблокирован:**
- T-37 убрал runtime crashes (asyncpg.Record .get())
- T-38 убрал semantic bugs (version skew, dry-run mutation, side ledger)
- Чего ещё может найти round 3: скорее всего observability/monitoring дыры (Telegram-alert на watchdog restart, на circuit breaker trip, etc.) — улучшения а не блокеры

---

### T-37 · Fix asyncpg.Record misuse (codex adversarial review 2026-04-16) ✅
**Status:** DONE — 2026-04-16
**Источник:** [obsidian/00_inbox/codex-adversarial-review-2026-04-16.md](obsidian/00_inbox/codex-adversarial-review-2026-04-16.md) — verdict: NO-SHIP, 3 HIGH + 1 MED
**Время:** ~1 час

**Что было сломано:**
- `asyncpg.Record` **не поддерживает `.get()`** (только subscripting). 3 HIGH findings от codex — все падали бы с AttributeError при первой же открытой позиции: `risk_guard` filter, `order_poller._handle_exit_pending`, `telegram_commands` (/positions, digest, heartbeat).
- `main.py` supervisor использовал `asyncio.gather(return_exceptions=True)` на never-ending loops — exceptions попадали бы в `results`, но inspection code выполнился бы **только** после завершения всех loops (никогда). Collector работал бы «наполовину живой» без видимости.
- Мои 15 unit-тестов прошли потому что моки использовали обычный `dict`, а не настоящий Record-like object.

**Fix B — normalize at fetch** ([trading/position_manager.py](trading/position_manager.py)):
```python
async def get_open_positions(pool) -> list[dict]:
    rows = await pool.fetch("SELECT * FROM open_positions WHERE status='open'...")
    return [dict(r) for r in rows]   # одна точка конверсии
```
Все downstream callers (risk_guard, order_poller, telegram_commands, exit_monitor) продолжают работать как с dict — `.get()` теперь safe.

**Collector supervisor rework** ([main.py](main.py)):
- `asyncio.create_task(...)` для каждого из 4 loops
- `asyncio.wait(tasks, return_when=FIRST_EXCEPTION)` — если любой loop умирает, whole process exits
- `try/finally` cancel pending tasks + `gather(return_exceptions=True)` чтобы подождать cancellation
- watchdog.sh подхватит и рестартанёт — чище чем половинчатое состояние

**Новые тесты** ([tests/test_position_manager_records.py](tests/test_position_manager_records.py)):
- `RecordLike` fixture — mimics asyncpg.Record (subscript-only, no `.get()`)
- 5 unit-тестов, включая contract test что `get_open_positions` возвращает dicts, и negative test что `.get()` на RecordLike сам по себе raises AttributeError
- **Любой будущий автор** который напишет `.get()` на row — сразу получит AttributeError в тестах, не в продакшене

**Verification:**
- **107/107 tests** проходят: position_manager_records (5 новых) + resolve_team_token (15) + risk_guard (33) + telegram_commands + trading
- Syntax check main.py + position_manager.py: OK
- Следующий шаг: запустить `/codex:adversarial-review` снова — verdict должен быть "green" или "needs-attention" только с новыми findings, не теми же.

**Lessons learned (в `/finish-task` обсидиан):**
- Dict-based mocks are too convenient — modelируй реальную semantics mock'ов (subscript-only для asyncpg.Record)
- `asyncio.gather(return_exceptions=True)` + never-ending tasks = silent failure. Используй `asyncio.wait(FIRST_EXCEPTION)` для supervision pattern.
- Adversarial review ловит то, что unit-тесты с mock'ами пропускают — runtime type contracts.

---

### T-34 · Collector resilience + auto-restart ✅
**Status:** DONE — 2026-04-16
**Первоначальный scope:** "добавить MLB market discovery в collector". Это оказалось **уже готово** — [collector/market_discovery.py](collector/market_discovery.py) имеет `"mlb"` в `SPORTS_LEAGUES`, `"baseball"` в `TRADEABLE_SPORTS`, `"mlb-"` в `_SLUG_PREFIX_MAP`. Mac Mini DB содержит 30 MLB рынков с сотнями snapshots на каждый (2026-04-14, все settled).

**Реальная проблема:** коллектор (`main.py`) крашнулся ~28h назад с `TimeoutError` при `insert_gap` → asyncpg pool не смог получить connection → exception пропагировался через `asyncio.gather` и убил весь процесс. НИЧЕГО не supervise'ило его — `watchdog.sh` только наблюдал за `bot.pid`, `start_bot.sh` его даже не запускал.

**Что сделано:**

1. **B — Hardening [main.py](main.py):**
   - `_snapshot_loop` каждая per-market итерация обёрнута в `try/except` — один failing market больше не убивает весь loop
   - Каждый DB call (update_market_status, insert_gap, insert_snapshot) завёрнут в свой try/except с `logger.warning` на failure
   - `asyncio.gather(..., return_exceptions=True)` в `start()` — если один из 4-х loops (snapshot/rescan/heartbeat/ws) полностью умирает, остальные продолжают работать
   - Gap insert + alert вызовы — best-effort: не критичны, не валят процесс

2. **A — Auto-restart [scripts/watchdog.sh](scripts/watchdog.sh) + [scripts/start_bot.sh](scripts/start_bot.sh):**
   - Коллектор теперь first-class supervised service — `collector.pid` + `collector.log`
   - `start_bot.sh` добавил `main.py` запуск перед bot'ом
   - `watchdog.sh` получил зеркальный блок supervision с отдельным `collector_restart_count` (не делит counter с ботом)
   - `stop_bot.sh` знает про `collector.pid` → `make stop` чистит всё

**Следующий session на Mac Mini:**
- `make stop && make start` — применит новую версию коллектора + запустит его под watchdog
- После restart: `tail -f logs/collector.log` покажет pickup MLB (если есть сегодняшние игры)
- Проверить через ~10 мин: `SELECT sport, COUNT(*) FROM price_snapshots WHERE ts > NOW() - INTERVAL '10 minutes' GROUP BY 1`

**Не сделано (out-of-scope):**
- Sport-specific filter relaxation (MLB часто имеет volume < $10k) — отдельная задача если захотим больше MLB данных
- Observability: Telegram-alert когда watchdog реально restart'ит collector — сейчас только в logs/watchdog.log

---

## Completed (this sprint)

- **T-19** MLB Pitcher Scanner built: `collector/mlb_data.py` + `analytics/mlb_pitcher_scanner.py` + `config/mlb_team_aliases.yaml` + DB table + config + Makefile + bot_main integration
- **T-20** DB schema deployed on Mac Mini (all 12 tables)
- **T-21** MLB scanner validated: switched to MLB Stats API, 50/53 pitchers enriched, 4 HIGH signals found
- **T-22** MLB scanner running in watch mode on Mac Mini, added to start_bot.sh autostart
