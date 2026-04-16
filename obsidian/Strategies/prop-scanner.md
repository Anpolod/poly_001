# Player Prop EV Scanner

> Статус: ✅ Production | Сезонность: весь NBA сезон (октябрь-апрель)

## Суть

Ищем player prop рынки (points/rebounds/assists), где историческая калибровка показывает систематический edge (actual win rate > market price). Входим за 6-12ч до tip-off.

## Реализация в коде

| Модуль | Роль |
|--------|------|
| `analytics/prop_scanner.py` | Скан + EV расчёт + daemon mode |
| `analytics/historical_fetcher.py` | Заполнение historical_calibration |
| `analytics/calibration_analyzer.py` | Калибровочная модель |
| `trading/bot_main.py` → `_process_prop_signal()` | Исполнение |

## Ключевые метрики

- Favorite-longshot bias: рынки с ценой 0.20 побеждают в ~28% случаев (edge +8%)
- Лучший EV: points O/U в диапазоне 0.30-0.50
- Daemon mode: каждые 5 мин, логирует в `prop_scan_log`, Slack алерты

## Что можно улучшить

1. Расширить на другие виды спорта (NHL, MLB)
2. Учитывать lineup confirmation (если звезда OUT → O/U на его стате бессмысленен)
3. Real-time P&L tracking по пропам (сейчас только simulated)
