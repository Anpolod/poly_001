# Cross-Sport Prematch Drift Monitor

> Статус: 🆕 Concept | Сезонность: круглый год

## Суть

Универсальный детектор drift'а на всех спортивных рынках. Если цена двигается > X% за Y часов без очевидного news event — это сигнал что смарт-деньги уже вошли. Можно ещё успеть.

## Механика

Drift бывает двух типов:
1. **Information-driven** — кто-то знает про травму/состав до официального объявления
2. **Flow-driven** — крупный ордер давит цену, остальные подтягиваются

Оба типа создают momentum, который продолжается 4-12 часов.

## Что уже есть в проекте

| Модуль | Что делает |
|--------|------------|
| `analytics/movement_analyzer.py` | Находит рынки с сильным drift'ом |
| `analytics/spike_vs_drift_report.py` | Классифицирует: spike (резкий) vs drift (плавный) |
| `collector/spike_tracker.py` | Real-time spike detection |
| `analytics/backtester.py` | DriftSignal + ReversionSignal backtest |

## Что нужно добавить

- **Real-time drift alerts** — сейчас movement_analyzer работает batch, нужен streaming
- **News correlation** — автоматически чекать, есть ли news event при drift'е (Rotowire, ESPN)
- **False positive filter** — drift из-за ликвидности (один крупный ордер) vs. из-за информации

## Логика

```
Каждые 15 мин:
1. Для каждого active market с event_start в ближайшие 48ч:
   - Δprice_6h = current - price_6h_ago
   - Δprice_3h = current - price_3h_ago
2. Если abs(Δprice_6h) > 0.04 AND abs(Δprice_3h) > 0.02:
   → DriftAlert(direction, magnitude, speed)
3. Фильтры:
   - Не алертить если уже есть spike_event за последний час (→ news-driven, не drift)
   - Не алертить если spread > 10% (неликвид)
```

## Приоритет

MEDIUM — полезно как overlay поверх других стратегий, но не standalone.
