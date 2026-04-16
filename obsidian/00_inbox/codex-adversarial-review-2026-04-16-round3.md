# Codex Adversarial Review — 2026-04-16 Round 3 (+ T-39 fixes)

**Источник:** `/codex:adversarial-review` третий запуск после T-38
**Объём:** 19 modified files, ~1,658 lines
**Thread:** `019d94f9-5cd3-79f1-b181-c6604b4935cb`
**Verdict round 3:** 🚨 needs-attention — NO-SHIP (1 HIGH + 1 MED)
**Status после T-39 fixes:** ✅ DONE 2026-04-16

---

## TL;DR

Round 3 нашёл **меньше** (2 vs 3 vs 4 в предыдущих раундах) — тренд к green verdict. Обе находки были реальные регрессии из T-35 и T-38 соответственно, не базовые дефекты.

Все пофикшены за ~45 мин + 2 новых теста.

---

## Финдинги (обе DONE)

### 🔴 [HIGH] #1 — DRY_ exits stuck в `exit_pending` навсегда ✅

**Файл:** [trading/order_poller.py:359-361](trading/order_poller.py#L359-L361)

**Проблема:**
T-35 ввёл exit_pending state machine. В `_handle_exit_pending`:
```python
if sell_order_id.startswith("DRY_"):
    # Dry-run sell — close immediately at the entry_price's negation
    return
```
Комментарий говорил "close immediately" — код просто return. Позиция навсегда залипает в exit_pending. Каждый dry_run stop-loss/take-profit → inflating exposure, blocking new entries via `has_position`, corrupting P&L/dashboard.

**Fix:**
```python
if sell_order_id.startswith("DRY_"):
    exit_price = float(pos.get("current_bid") or pos.get("entry_price") or 0)
    if exit_price <= 0:
        await mark_exit_failed(pool, position_id)  # revert, retry
        return
    pnl = await close_position(pool, position_id, exit_price)
    logger.info("DRY-RUN exit closed @ %.4f  P&L (simulated): %+.2f", exit_price, pnl)
    return
```

Симметрично с MATCHED path, использует `current_bid` как simulated fill price.

### 🟡 [MED] #2 — Watchdog restart budget leak ✅

**Файл:** [scripts/watchdog.sh:63-83](scripts/watchdog.sh#L63-L83)

**Проблема:**
- `bot_restart_count` / `collector_restart_count` инкрементятся на каждом restart, но **никогда не сбрасываются**
- T-38 добавил: `_git_sync` убивает оба процесса при каждом deploy → уменьшает budget на 1
- После 10 deploys (не крэшей!) watchdog перестаёт recover'ить процессы
- **Silent permanent outage** — следующий crash без ручного вмешательства = permanent downtime

**Fix:** consecutive-failure semantics.
```bash
HEALTHY_RESET_TICKS=10  # 5 min of continuous uptime

# On healthy check:
bot_healthy_ticks=$((bot_healthy_ticks + 1))
if [ "$bot_healthy_ticks" -ge "$HEALTHY_RESET_TICKS" ] && [ "$bot_restart_count" -gt 0 ]; then
  echo "bot stable for 5 min — resetting restart count (was $bot_restart_count)"
  bot_restart_count=0
fi

# On death:
bot_healthy_ticks=0
# ... existing restart logic (unchanged)
```

Logic:
- Deploy restart: процесс живёт > 5 мин → counter clears → infinite budget for стабильных процессов
- Real crash loop (умирает < 5 мин каждый раз): counter накапливается → eventually MAX_RESTARTS → give up (correct behavior)
- Self-correcting: temporary flakiness (network hiccup и т.п.) does not permanently lock budget

---

## Новые тесты (регрессия)

[tests/test_position_manager_records.py](tests/test_position_manager_records.py) — 2 новых:

1. **`test_dry_run_exit_closes_position_immediately`**
   - Mock'ает `close_position`, создаёт `pos` с `exit_order_id='DRY_SELL_xxx'` и `current_bid=0.37`
   - Assert'ит что `close_position(pool, 99, 0.37)` был awaited
   - Assert'ит что `executor.get_order` НЕ был awaited (DRY never hits CLOB)
   - Ловит future регрессию если кто-то опять сделает `return` в DRY branch

2. **`test_dry_run_exit_reverts_if_no_usable_price`**
   - Защитный тест: zero prices → не закрывать за $0 (fake loss)
   - Assert'ит что `mark_exit_failed(pool, 100)` вызывается вместо `close_position`

---

## Прогресс по раундам adversarial review

| Round | Findings | Severity | Категория | Cost to fix |
|-------|----------|----------|-----------|-------------|
| 1 | 4 | 3 HIGH + 1 MED | Runtime crashes (asyncpg.Record `.get()`) | ~1 час (T-37) |
| 2 | 3 | 1 HIGH + 2 MED | Deploy / dry-run mutation / side ledger | ~1 час (T-38) |
| 3 | 2 | 1 HIGH + 1 MED | Dry-run state regression + watchdog budget | ~45 мин (T-39) |
| 4 | TBD | ... | Наверное observability gaps, Telegram alerts | ? |

**Тренд:** 4 → 3 → 2 findings. Severity тоже падает (первый round был криминально плохой — бот упал бы при первой позиции). Round 3 — более nuanced design issues.

**Ship readiness:**
- Все 9 findings из 3 раундов закрыты
- 112/112 unit tests passing
- Есть regression tests для каждого класса багов (asyncpg.Record, dry-run no-mutate, DRY exit close, side persistence)

**Надо ли round 4?**
- Pro: последняя шлифовка, потенциально green verdict
- Con: stakeholder fatigue + diminishing returns — каждый раунд находит меньше и менее criticалных проблем
- **Мой совет:** ship сейчас. Live trading мы ещё не флипаем (`trading.enabled: false`), и round 4 можно прогнать после реального deploy если захотим — найдёт проблемы видимые только в production traffic.

---

## Метаданные

- **Стоимость:** $0 (ChatGPT Plus)
- **Время на review:** ~4 мин
- **Времeni на T-39 fix:** ~45 мин
- **Кодекс нашёл:** 2 (vs 3, vs 4)
- **Regression tests added:** 2 (total 112/112)
- **Status:** готово к `/ship-to-macmini` через 3 раунда adversarial review
