# Codex Review — 2026-04-15

**Источник:** `/codex:review` против working tree (T-28 Injury Scanner + T-29 Calibration Trader + 3 новых skill файла)
**Объём:** 17 modified files, ~1015 строк изменений + новые модули
**Reviewer:** OpenAI Codex (через codex-plugin-cc, ChatGPT auth)
**Тред:** `019d92f7-a9a5-7903-9e4f-89fafc564b29`

---

## TL;DR

Patch добавляет несколько новых торговых и сигнальных путей, но **некоторые из них могут торговать по неправильной стороне рынка** или **записывать выходы до того, как sell-ордер реально исполнился**. Это приводит к: некорректным алертам, неправильно открытым позициям, и рассинхрону портфельного состояния — даже если остальная система работает корректно.

---

## Найденные проблемы

### 🔴 P1 — Выбирать сторону токена по favored MLB команде

**Файл:** [trading/bot_main.py:417-418](trading/bot_main.py#L417-L418)

Когда питчер-фаворит относится к `NO`-стороне рынка, текущий код всё равно покупает через `_get_token_id(...)`, а эта функция всегда возвращает `token_id_yes`. В таких случаях бот занимает **противоположную** сторону сигнала и покупает аутсайдера вместо favored команды.

**Это не редкий edge case** — проявляется на любом MLB рынке, где вопрос сформулирован про другую команду.

**Что делать:** определять token_id_yes vs token_id_no на основе того, какая команда (favored или underdog) соответствует YES-стороне в `markets.question`/`slug`.

---

### 🔴 P1 — Не помечать stop-loss выходы closed до фактического fill

**Файл:** [trading/risk_guard.py:125-127](trading/risk_guard.py#L125-L127)

`executor.sell()` размещает GTC limit-ордер и может вернуть `LIVE`/`unfilled`, но текущий код реализует P&L и закрывает позицию **сразу после** этого вызова. Если best bid исчезает или ордер просто висит на book — БД утверждает, что позиция закрыта, и бот **перестаёт её мониторить**, хотя shares фактически всё ещё в портфеле.

Та же проблема и для **take-profit**, и для **stop-loss** ветвей.

**Что делать:** не вызывать `close_position` синхронно. Помечать позицию как `fill_status='exit_pending'` и доверять `order_poller` обновить статус, когда sell реально исполнится.

---

### 🔴 P1 — Не считать market YES price ценой здоровой команды в injury scanner

**Файл:** [analytics/injury_scanner.py:266-281](analytics/injury_scanner.py#L266-L281)

`current_price` здесь — это всегда YES-цена рынка, но сигнал помечает рекомендуемую сторону как `healthy_team` без проверки, является ли эта команда YES или NO в вопросе рынка. Для **примерно половины** матчей `action='BUY'` и сохранённые `price`/`drift` будут ссылаться на **неправильную** сторону.

В результате scanner может репортить injury edge как "дешёвый" или "дорогой" на основе цены injured team's контракта вместо healthy team's.

**Что делать:** определять, какая сторона рынка (YES/NO) соответствует healthy_team, и подставлять соответствующую цену (`1 - mid_price` если healthy_team на NO).

---

### 🟡 P2 — Фильтровать calibration edges по market_type до выбора specific rows

**Файл:** [analytics/calibration_signal.py:331-337](analytics/calibration_signal.py#L331-L337)

Цикл предпочитает любую non-`any` edge запись, которая матчит sport+price bucket, но **никогда не проверяет**, совпадает ли `market_type` этой edge с текущим рынком.

Если `calibration_edges` был построен с `--per-market-type`, то totals или spreads рынок может **унаследовать moneyline edge** просто потому, что у них одинаковый sport и price range. Это даст ложные `BUY_YES`/`BUY_NO` алерты.

**Что делать:** в loop добавить проверку `if e.market_type == r['market_type'] or e.market_type == 'any'` перед `match` присваиванием.

---

### 🟡 P2 — Передавать MLB ROI в position sizing в процентах, не в долях

**Файл:** [trading/bot_main.py:380-382](trading/bot_main.py#L380-L382)

`position_size_by_ev()` ожидает ROI **в процентных пунктах** (в тех же единицах, что и props/tanking — например `5.0` для 5%), но MLB pitcher path передаёт `0.02`/`0.03` (доли).

В результате каждая pitcher сделка попадает ниже `ev_min_roi_pct` и получает минимальный размер. **Новая MLB стратегия никогда не увеличивает размер** даже для самых сильных mismatches.

**Что делать:** заменить `roi_est = max(0.02, signal.era_differential * 0.01)` на что-то вроде `roi_est = max(2.0, signal.era_differential * 1.0)` (в процентных пунктах, не долях).

---

## Приоритеты для исправления

| # | Severity | Файл | Время на фикс |
|---|---|---|---|
| 1 | 🔴 P1 | `trading/risk_guard.py` (stop-loss закрывает до fill) | ~30 мин — переписать на `exit_pending` + положиться на order_poller |
| 2 | 🔴 P1 | `analytics/injury_scanner.py` (YES/NO mapping) | ~45 мин — нужна вспомогательная функция `resolve_team_side(market, team_name)` |
| 3 | 🔴 P1 | `trading/bot_main.py` MLB pitcher path (token side) | ~30 мин — та же `resolve_team_side` функция |
| 4 | 🟡 P2 | `analytics/calibration_signal.py` (market_type filter) | ~10 мин — добавить проверку в loop |
| 5 | 🟡 P2 | `trading/bot_main.py` MLB ROI units | ~5 мин — заменить fraction на percent |

**Общий блок 1 (всё P1):** ~2 часа. Без этого нельзя флипать `trading.enabled = true` — иначе deploy торговать неправильную сторону рынка.

**Общий блок 2 (P2):** ~15 мин. Можно добить вместе с P1.

---

## Что общего между P1 #2 и #3 (token side detection)

Обе проблемы решает одна и та же функция:

```python
async def resolve_team_token_id(
    pool, market_id: str, team_canonical: str
) -> tuple[str, float]:
    """Возвращает (token_id, side_price) для конкретной команды в рынке.
    side_price = mid_price если команда == YES, иначе (1 - mid_price).
    """
    ...
```

После её добавления:
- `injury_scanner.build_injury_signals()` использует её для расчёта `current_price` от healthy_team
- `bot_main._process_pitcher_signal()` использует её для выбора правильного `token_id`
- Бонус: можно переиспользовать в `tanking_scanner` (там та же латентная проблема, но мы её не зафиксили)

---

## Метаданные

- **Стоимость:** $0 (ChatGPT Plus subscription)
- **Время на review:** ~3 минуты (codex прочитал ~25 файлов через rg/sed)
- **Следующий шаг:** создать отдельные T-## задачи под каждый P1 fix, или сгруппировать как T-35 "Pre-live trading audit fixes"
