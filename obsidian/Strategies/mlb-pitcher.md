# MLB Pitcher Differential

> Статус: 🔧 Built, awaiting deploy | Сезонность: апрель-октябрь (MLB regular season)

## Суть

В бейсболе стартовый питчер (SP) имеет огромное влияние на исход. Probable Pitchers публикуются за 2-3 дня, но Polymarket реагирует медленно. Покупаем фаворита когда ace (ERA < 3.0) идёт против слабого питчера (ERA > 5.0).

## Почему должно работать

- SP объясняет ~25-30% вариации исхода матча MLB (больше, чем любой фактор в баскетболе)
- Probable Pitchers — публичная информация, но Polymarket ордербук ленив
- Аналогичная механика drift'а: информация публична → рынок реагирует с задержкой → мы входим в окне

## Источники данных

| Источник | Данные | Частота |
|----------|--------|---------|
| ESPN API | Probable pitchers, game schedule | 2x/day |
| FanGraphs / Baseball Reference | ERA, WHIP, WAR, K/9 | Daily |
| Rotowire | Lineup confirmations | Day of game |
| Polymarket CLOB | Цены, ордербуки | Real-time (уже есть) |

## Логика сигнала

```
Каждые 12 часов:
1. Fetch probable pitchers для игр в ближайшие 48ч
2. Для каждого матча:
   - SP_home ERA, SP_away ERA
   - pitcher_differential = abs(ERA_home - ERA_away)
3. Если pitcher_differential > 2.0 AND market_price < 0.70:
   → PitcherSignal(team_with_better_SP, entry_price)
4. Дополнительные фильтры:
   - Bullpen freshness (pitch counts за 3 дня)
   - Home/Away ERA split
   - Recent form (last 5 starts)
```

## Реализация в коде

| Модуль | Роль |
|--------|------|
| `collector/mlb_data.py` | ESPN API — расписание, probable pitchers, сезонные статы |
| `analytics/mlb_pitcher_scanner.py` | Scanner: ERA differential → signal → Polymarket matching |
| `config/mlb_team_aliases.yaml` | 30 MLB команд, все алиасы |
| `trading/bot_main.py` → `_process_pitcher_signal()` | Исполнение через бот |
| `config/settings.yaml` → `mlb_pitcher_scanner` | Пороги и настройки |
| DB: `pitcher_signals` | Логирование сигналов |

### CLI
```bash
python -m analytics.mlb_pitcher_scanner                    # one-shot scan
python -m analytics.mlb_pitcher_scanner --min-era-diff 1.5 # stricter filter
python -m analytics.mlb_pitcher_scanner --watch --save     # continuous + DB
make mlb                                                    # alias
```

## Риски

- Бейсбол более рандомен чем баскетбол (1 матч ≈ coin flip даже с ace)
- Polymarket может не иметь достаточную ликвидность на MLB
- Нет исторических данных для бэктеста (нужно собирать с нуля)

## Приоритет

**HIGH** — MLB сезон уже начался, окно для сбора данных и тестирования открыто прямо сейчас.
