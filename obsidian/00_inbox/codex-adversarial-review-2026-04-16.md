# Codex Adversarial Review — 2026-04-16

**Источник:** `/codex:adversarial-review` против working tree
**Объём:** 19 modified files, ~1,536 lines + новые модули (injury/calibration/drift/spike scanners + T-34 collector resilience + T-35 exit flow)
**Reviewer:** OpenAI Codex (через codex-plugin-cc, ChatGPT auth)
**Тред:** `019d94e4-7fcd-7493-9ce2-97f0d8e937ca`
**Verdict: 🚨 needs-attention — NO-SHIP**

---

## TL;DR

Кодекс не одобряет push в текущем виде. Изменения вводят **несколько runtime failures** в critical trading/control paths:
- core background tasks **тихо умирают** без видимости
- **stop-loss и take-profit отключаются** при любой открытой позиции
- **matched exit orders никогда не финализируются** → позиция остаётся `exit_pending` навсегда
- **Telegram команды/отчёты падают** при открытых позициях → нет мониторинга когда он нужнее всего

Корневая причина **трёх из четырёх** находок — одна и та же ошибка: `.get()` вызывается на `asyncpg.Record`, а Record — НЕ dict, он subscriptable (`record["field"]`) но `.get()` не поддерживает. Тесты прошли потому что моки возвращали обычные dict.

---

## Критические находки (все должны быть пофикшены до flip на live trading)

### 🔴 [HIGH] #1 — Collector silent death через `gather(return_exceptions=True)`

**Файл:** [main.py:83-97](main.py#L83-L97)

**Проблема:**
T-34 заменил `asyncio.gather(...)` на `asyncio.gather(..., return_exceptions=True)` чтобы один умерший loop не ронял остальные. Но я инспектирую exceptions **только после** завершения gather. Эти loops бесконечные — они **никогда не завершаются** нормально. Значит:

- Если один loop умирает (например snapshot_loop падает от бага) — exception capture'ится в `results`
- Но `gather` продолжает ждать остальные 3 живых loops
- Inspection кода `for i, r in enumerate(results)` **никогда не выполнится** пока все 4 не завершатся
- Collector работает с мёртвым WS / snapshots / rescan, **выглядя здоровым** в логах

Это точно та категория partial-failure, от которой T-34 должен был защитить — и которую сам же породил.

**Fix:**
Создавать explicit `asyncio.create_task()` и использовать `asyncio.wait(..., return_when=FIRST_EXCEPTION)` ИЛИ добавить `task.add_done_callback(...)` которые логируют + триггерят shutdown при смерти любого task. Нельзя полагаться на `gather(return_exceptions=True)` для никогда-не-завершающихся loops.

---

### 🔴 [HIGH] #2 — Stop-loss/take-profit падает при любой открытой позиции

**Файл:** [trading/risk_guard.py:91-98](trading/risk_guard.py#L91-L98)

**Проблема:**
```python
filtered = [
    p for p in positions
    if p["token_id"]
    and p["clob_order_id"]
    and (p.get("fill_status") or "pending") == "filled"  # ← СЛОМАНО
]
```

`get_open_positions()` возвращает `asyncpg.Record` rows, а `Record.get()` **не существует**. Как только у бота появляется **хотя бы одна** открытая позиция — list comprehension падает с AttributeError ДО любых risk checks. Outer `except` ловит, пишет ERROR в лог, и monitor спит interval. На следующем цикле то же самое → stop-loss и take-profit **никогда не срабатывают**.

**Impact:** live позиции остаются **без защиты** во время adverse moves. Если рынок двигается против нас — 40% stop-loss просто не сработает. Катастрофично для live trading.

**Fix:**
Заменить `p.get("fill_status")` на `p["fill_status"]` (asyncpg Record supports subscripting). Альтернатива — преобразовать rows в `dict(r)` при fetch. Добавить integration test который использует реальный asyncpg.Record (или Mock с subscript-only interface) для этого пути.

---

### 🔴 [HIGH] #3 — Matched exit orders никогда не финализируются

**Файл:** [trading/order_poller.py:371-382](trading/order_poller.py#L371-L382) (T-35 `_handle_exit_pending`)

**Проблема:**
Когда exit SELL доходит до MATCHED на CLOB, `_handle_exit_pending` пытается вычислить `exit_price`:

```python
exit_price = float(pos.get("current_bid") or pos.get("entry_price") or 0)  # ← ПАДАЕТ
```

`pos` — asyncpg.Record, `.get()` не работает. AttributeError **до вызова `close_position()`**. Broad `except` на уровне cycle ловит, retry на следующем poll — но проблема детерминированная, она повторится **всегда**.

**Impact:** позиция остаётся в `fill_status='exit_pending'` **навсегда**, хотя биржа shares уже продала. Portfolio state расходится с реальностью:
- Closed risk считается как open → exposure limits срабатывают неправильно
- Daily P&L не учитывает реализованный убыток → circuit breaker даёт ложные clearances
- Deployments идут с позициями которые давно не существуют

Именно та race condition которую T-35 должен был **решить** — и вместо этого ввёл новую.

**Fix:**
`pos["current_bid"]` / `pos["entry_price"]` вместо `.get()`. Обязательно добавить тест, который прогоняет MATCHED exit path с реальным row-like object и проверяет что позиция помечается closed.

---

### 🟡 [MEDIUM] #4 — Telegram control/reporting падает по той же причине

**Файл:** [trading/telegram_commands.py:135-145](trading/telegram_commands.py#L135-L145)

**Проблема:**
Та же `p.get(...)` на asyncpg.Record в:
- `/positions` команда — падает до рендеринга первой позиции
- `_build_digest()` — scheduled digests перестают приходить
- `heartbeat()` — ping может падать

**Impact:** operator visibility исчезает **именно когда** она нужнее всего. Никаких Telegram updates → не видно что stop-loss broken (находка #2), не видно что exit stuck (находка #3), circuit-breaker считает неправильно. Полная потеря observability.

**Fix:** та же замена `.get()` на subscripting + command-path test с реальным row type.

---

## Почему это не поймали unit-тесты

**15/15 моих тестов в [tests/test_resolve_team_token.py](tests/test_resolve_team_token.py) прошли** — но они все использовали:
```python
pool.fetchrow = AsyncMock(return_value={"slug": ..., "token_id_yes": ...})
```

Обычный `dict`, не asyncpg.Record. **`.get()` на dict работает.** Мои моки были too convenient — они моделировали что я ожидаю, а не что asyncpg реально возвращает.

**Урок:** для кода который работает с asyncpg результатами — mock'ать нужно object поведение реального asyncpg.Record (subscript-only, no .get), а не подставлять dict.

---

## Priorities

| # | Severity | Файл | Cost | Blocks |
|---|---|---|---|---|
| 1 | 🔴 HIGH | `trading/risk_guard.py:91-98` | ~10 мин | **live trading safety** |
| 2 | 🔴 HIGH | `trading/order_poller.py:371-382` | ~10 мин | **portfolio state integrity** |
| 3 | 🔴 HIGH | `main.py:83-97` | ~30 мин | collector observability |
| 4 | 🟡 MED  | `trading/telegram_commands.py:135-145` | ~15 мин | operator visibility |
| + | – | `tests/*` — добавить asyncpg.Record-like тесты | ~30 мин | катч future regression |

**Общий cost:** ~1.5 часа. **БЛОКЕР для live trading.**

---

## Общее решение

Все 3 из 4 находок — один pattern: `Record.get()`. Два варианта фикса:

**A) Indexed access везде** (точечный fix):
```python
fill_status = (p["fill_status"] if "fill_status" in p else None) or "pending"
# или: (p["fill_status"] or "pending")  — работает если колонка всегда присутствует
```

**B) Normalize at fetch time** (более робастный):
```python
async def get_open_positions(pool) -> list[dict]:
    rows = await pool.fetch("SELECT * FROM open_positions WHERE status='open' ORDER BY entry_ts")
    return [dict(r) for r in rows]  # ← добавить конверсию
```

**Рекомендую B** — одна точка конверсии, всё downstream работает как с dict, и unit-тесты с dict-моками становятся honest. Downsides: теряем asyncpg.Record performance (marginal), нужно аудитить все функции которые используют Records (grep'ом за 5 мин).

---

## Следующий шаг — T-37 ✅ DONE 2026-04-16

**Fix B применён** — [trading/position_manager.py:82-92](trading/position_manager.py#L82-L92) `get_open_positions` теперь возвращает `list[dict]`, все 7+ downstream `.get()` вызовов работают корректно без изменений.

**Supervisor переписан** — [main.py:83-111](main.py#L83-L111) использует `asyncio.wait(return_when=FIRST_EXCEPTION)` вместо `gather(return_exceptions=True)`, с `try/finally` cancellation.

**Test hardening** — [tests/test_position_manager_records.py](tests/test_position_manager_records.py): 5 unit-тестов с `RecordLike` fixture (subscript-only, no `.get()`). Contract test гарантирует что `get_open_positions` возвращает dicts, не Records. Любой будущий `.get()` на raw row — AttributeError в тестах.

**Итого:** 107/107 тестов проходят. Готово для повторного `/codex:adversarial-review`.

- [x] ~~Решить A vs B~~ → B (one-point normalization)
- [x] ~~Применить фикс~~ → position_manager.get_open_positions
- [x] ~~Переписать supervisor~~ → asyncio.wait(FIRST_EXCEPTION)
- [x] ~~Добавить тесты~~ → tests/test_position_manager_records.py (5 tests)
- [ ] Rerun `/codex:adversarial-review` — проверить что verdict перешёл с NO-SHIP
- [ ] Только потом flip `trading.enabled: true`

---

## Метаданные

- **Стоимость:** $0 (ChatGPT Plus)
- **Время на review:** ~4 минуты
- **Codex нашёл:** 4 проблемы (3 HIGH + 1 MED), все реальные баги
- **Моих unit-тестов прошло:** 48/48 — ни один не поймал эту группу багов (все 4 — runtime-only, dict-mock хиды их)
- **Verdict для ship-to-macmini:** НЕТ. Сначала T-37 fix, потом push.
