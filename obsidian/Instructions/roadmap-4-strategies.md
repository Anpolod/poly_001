# Roadmap: Стратегии развития

> Обновлено: 2026-04-14 | Статус: активный план

---

## Инвентаризация: что есть

### ✅ Работает в Production

| Компонент | Модуль | Описание |
|-----------|--------|----------|
| Data Pipeline | `main.py` | 24/7 коллектор: snapshots 5 мин, WS trades, rescan 1ч |
| Cost Analysis | `cost_analyzer.py` | Phase 0 batch: spread/depth/ratio → GO/MARGINAL/NO_GO |
| **Tanking Scanner** | `analytics/tanking_scanner.py` | NBA motivation differential → BUY/WATCH/SELL |
| **Prop Scanner** | `analytics/prop_scanner.py` | Player prop EV (daemon + one-shot) |
| Trading Bot | `trading/bot_main.py` | Tanking + Prop signals → Telegram → CLOB execution |
| CLOB Executor | `trading/clob_executor.py` | py-clob-client async wrapper, buy/sell/cancel |
| Risk Management | `trading/risk_guard.py` + `risk_manager.py` | Stop-loss, circuit breaker, correlation, position sizing |
| Entry Filter | `trading/entry_filter.py` | Liquidity + spread + timing checks |
| Exit Monitor | `trading/exit_monitor.py` | Auto-exit перед игрой |
| Telegram | `trading/telegram_confirm.py` + `telegram_commands.py` | Алерты, подтверждения, /status, /digest |
| Dashboard | `dashboard/main_dashboard.py` | Streamlit: Overview, Markets, Alerts, Cost, Tanking, Health |
| DB | PostgreSQL + TimescaleDB | 8 таблиц, hypertables, все индексы |

### 📊 Аналитика (batch, не automated)

| Модуль | Что делает | Статус |
|--------|------------|--------|
| `movement_analyzer.py` | Drift detection по всем рынкам | CLI only |
| `spike_vs_drift_report.py` | Классификация: spike vs smooth drift | CLI only |
| `timing_analyzer.py` | Per-market price chart | CLI only |
| `backtester.py` | DriftSignal + ReversionSignal replay | CLI only |
| `calibration_analyzer.py` | Price vs actual outcome | CLI only |
| `historical_fetcher.py` | Fetch resolved markets | Run once |
| `obsidian_reporter.py` | Markdown reports → Obsidian | CLI/make |

### 🔧 Инфраструктура

- CI: GitHub Actions (lint + typecheck + test)
- Tests: 84 passing (pytest)
- Alerts: Slack webhook (collector events)
- Config: YAML-driven, env-var overrides for DB
- Logging: rotating file + console, per-module

---

## Что нужно добавить (приоритизированный план)

### Phase A — Оптимизация существующего (1-2 дня)

> Цель: выжать максимум из того, что уже работает

| # | Задача | Модуль | Impact |
|---|--------|--------|--------|
| A1 | **Hourly drift backtest для tanking** — найти T-optimal entry/exit | `tanking_scanner.py` + новый отчёт | HIGH |
| A2 | **P&L аудит** — посчитать реальный средний profit/loss с учётом spread | query к `prop_scan_log` + `price_snapshots` | HIGH |
| A3 | **Back-to-back filter** — пропускать если мотивированная команда играла вчера | `tanking_scanner.py` | MED |
| A4 | **Dynamic exit trigger** — выход по стагнации цены (3 flat snapshots) | `trading/exit_monitor.py` | MED |

### Phase B — MLB Pitcher Scanner (3-5 дней)

> Цель: перенести логику "мотивационного/информационного edge" на бейсбол. Сезон уже идёт.

| # | Задача | Новый файл | Зависимости |
|---|--------|------------|-------------|
| B1 | ESPN API: probable pitchers + schedule | `collector/mlb_data.py` | aiohttp (есть) |
| B2 | Pitcher stats fetcher (ERA, WHIP, recent form) | `collector/mlb_data.py` | FanGraphs/ESPN API |
| B3 | Scanner logic: pitcher_differential → signal | `analytics/mlb_pitcher_scanner.py` | B1, B2 |
| B4 | DB таблица `pitcher_signals` | `db/schema.sql` | — |
| B5 | Интеграция в bot_main | `trading/bot_main.py` | B3, B4 |
| B6 | Сбор данных + paper trading (2 недели) | — | B1-B5 |
| B7 | Бэктест на собранных данных | `analytics/mlb_backtester.py` | B6 |

### Phase C — Injury Scanner (2-3 дня)

> Цель: ловить drift от неожиданных травм ключевых игроков

| # | Задача | Описание |
|---|--------|----------|
| C1 | `analytics/injury_scanner.py` | Rotowire scraping (уже есть в tanking_scanner), парсинг PPG |
| C2 | DB: `injury_signals` | Новая таблица |
| C3 | Дедупликация + stale price check | Если рынок уже сдвинулся >10% → пропустить |
| C4 | Интеграция в bot_main | Новый scan cycle каждые 10 мин |
| C5 | Multi-sport: расширить на NHL, MLB | Разные URL'ы Rotowire |

### Phase D — Calibration Trader (1-2 дня)

> Цель: автоматизировать favorite-longshot bias

| # | Задача | Описание |
|---|--------|----------|
| D1 | `analytics/calibration_signal.py --build` | Edge таблица из historical_calibration |
| D2 | DB: `calibration_edges` | Новая таблица |
| D3 | Online scan: active markets vs edge table | Каждые 60 мин |
| D4 | Интеграция в bot_main | Signal → confirm → execute |

### Phase E — Real-time Drift Monitor (2-3 дня)

> Цель: кросс-спортивный overlay — алерт при подозрительном движении

| # | Задача | Описание |
|---|--------|----------|
| E1 | Streaming drift detector | На основе movement_analyzer, но real-time |
| E2 | News correlation | Чекать Rotowire/ESPN при drift'е |
| E3 | Slack + Telegram alerts | При drift > 4% за 6ч |
| E4 | False positive filter | Отличать liquidity event от information event |

### Phase F — Spike Follow (2 дня)

> Цель: следовать за smart money после spike events

Уже описано в предыдущем roadmap. Используем `spike_tracker.py` + `spike_events` таблицу. Добавляем `analytics/spike_signal.py` + интеграцию через asyncio.Queue.

### ❄️ Deferred — Market Maker

Отложено до тех пор, пока directional стратегии не стабилизируются. Требует другой инфраструктуры (двусторонние ордера, inventory management).

---

## Что УБРАТЬ / упростить

| Что | Почему | Действие |
|-----|--------|----------|
| `PROJECT_STATE.md` | Устарел (написан 2026-04-04), дублирует CLAUDE.md | Удалить или объединить с CLAUDE.md |
| `TASKS.md` | Все 18 задач завершены, файл мёртвый | Переместить в `docs/completed-tasks.md` |
| `Analysis.md` | Разовый отчёт | Переместить в `obsidian/00_inbox/` |
| `phase0_*.csv/json/md` | Phase 0 артефакты в корне | Переместить в `docs/phase0/` |
| `Untitled.base` | ? | Удалить если не нужен |
| `start_collector.bat` / `setup_autostart.ps1` | Windows-only, работа на Mac Mini | Проверить актуальность |

---

## Timeline

```
Апрель 14-15:  Phase A (оптимизация tanking + P&L аудит)
Апрель 16-20:  Phase B (MLB pitcher scanner — MVP)
Апрель 21-23:  Phase C (injury scanner)
Апрель 24-25:  Phase D (calibration trader)
Апрель 26-28:  Phase E (drift monitor)
Апрель 29-30:  Phase F (spike follow)
Май 1-14:      Paper trading + backtesting всех новых стратегий
Май 15+:       Production с реальными деньгами
```

---

## Архитектура сигналов (общая)

```
                    ┌─ tanking_scanner ──────┐
                    ├─ prop_scanner ─────────┤
                    ├─ mlb_pitcher_scanner ──┤
Data Sources ──────►├─ injury_scanner ───────┤──► Signal Queue ──► entry_filter
(REST, WS, ESPN)   ├─ calibration_signal ───┤       │                  │
                    ├─ drift_monitor ────────┤       │              risk_guard
                    └─ spike_signal ─────────┘       │                  │
                                                     │           Telegram confirm
                                                     │                  │
                                                     └──────► ClobExecutor.buy()
                                                                       │
                                                              exit_monitor / stop_loss
                                                                       │
                                                              ClobExecutor.sell()
```

Каждый scanner — независимый модуль с:
- Своим scan interval
- Своей DB таблицей для signals
- Своим config section
- CLI mode (`--dry-run`) + daemon mode
- Интеграцией в `bot_main.py` через общий signal interface
