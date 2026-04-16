# Codex Adversarial Review — 2026-04-16 Round 5 (+ T-41 fixes)

**Источник:** `/codex:adversarial-review` пятый запуск после T-40
**Объём:** 19 modified files, ~1,764 lines
**Thread:** `019d951e-1b01-79d0-bdd2-7e5bd998f419`
**Verdict round 5:** 🚨 needs-attention — NO-SHIP (2 HIGH + 1 MED)
**Status после T-41 fixes:** ✅ DONE 2026-04-16

---

## TL;DR

Round 5 нашёл 3 находки (первый рост после 4→3→2→2). Причина — ползая по краевым case'ам: rejected order paths, watchdog self-update, standalone MLB scanner. Это subtle bugs что rounds 1-4 пропустили потому что они фокусировались на более прямых flows.

Все 3 пофикшены за ~50 мин. Extracted pure helper `_is_buy_rejected` для DRY.

---

## Найденные проблемы

### 🔴 [HIGH] #1 — Rejected BUYs создавали ghost positions ✅

**Файл:** [trading/bot_main.py:322-338](trading/bot_main.py#L322-L338) (tanking path, + pitcher + prop)

**Проблема:**
Когда `executor.buy()` падает (rejected/insufficient balance/auth error/venue reject), возвращается `{"order_id": "", "status": "rejected", "error": "..."}`. Но все 3 buy paths (tanking, pitcher, prop) всё равно вызывали `open_position(clob_order_id="")`:

```python
order = await executor.buy(token_id, trade_price, size_usd)
# ↓ Никакой проверки!
actual_shares = order.get("size_shares", size_shares)
position_id = await open_position(
    ...
    clob_order_id=order.get("order_id", ""),  # ← Пустая строка для rejection
    ...
)
```

Дальше `order_poller._handle_no_order_id` видит `clob_order_id=""` и trigger'ит **retry buy**:
```python
if not order_id:
    await _handle_no_order_id(pos, pool, executor, tg_token, tg_chat_id)  # retries buy
```

Infinite retry loop на ghost position'е. Когда underlying issue clear'ится (user заливает balance, etc.), стартует **duplicate order** спам. `has_position()` блокирует новые legit entries потому что "already have one". P&L/exposure math искажены.

**Fix:** extracted pure helper + guard в 3 местах:
```python
def _is_buy_rejected(order: dict) -> tuple[bool, str]:
    order_id = order.get("order_id") or ""
    if order_id:
        return False, ""
    reason = order.get("error") or order.get("status") or "unknown"
    return True, str(reason)

# Каждый buy path:
order = await executor.buy(...)
rejected, reason = _is_buy_rejected(order)
if rejected:
    await log_order(pool, market_id, None, "buy_rejected", order)   # audit trail
    await send_error_alert(tg_token, tg_chat_id, f"BUY rejected: {reason}")
    return 0.0

order_id_str = order["order_id"]   # safe — guard passed
...
```

DRY_BUY_* (dry-run) не рассматривается как rejected — имеет non-empty id.

### 🔴 [HIGH] #2 — Watchdog не перезагружал сам себя ✅

**Файл:** [scripts/watchdog.sh:36-44](scripts/watchdog.sh#L36-L44)

**Проблема:**
`_git_sync()` был идеальный для детей (kill + restart loop подхватит их на новом коде), но **сам watchdog.sh shell process** уже прочитал старый script в память. `git reset --hard` на диске не обновляет running bash. После pull:
- Daemons рестартятся на новой версии ✅
- Watchdog работает старой логикой ❌
- На следующем deploy'е — та же история, но уже используя pre-T-40 supervision (i.e. ТОЛЬКО 2 daemons supervised)

Это анти-deploy-fix: коммит "fix watchdog supervision" никогда не activate'ится через auto-pull.

**Fix:** после kill children — `exec "$0" "$@"`. Это заменяет текущий bash process новой invocation'ом того же script'а. Same PID (watchdog.pid stays valid), same children (они survived exec — они были detached via nohup), но теперь running new bash code.

```bash
for name in "${DAEMON_NAMES[@]}"; do
  # ... kill each daemon pid ...
done
sleep 2
# Round-5 fix: reload self with new code
echo "$(date '+%Y-%m-%d %H:%M:%S') Re-executing watchdog with new code" >> "$SYNC_LOG"
exec "$0" "$@"
```

Children были just-killed; fresh watchdog видит dead pidfiles и restart'ит их на новой версии.

### 🟡 [MED] #3 — MLB scanner игнорировал side для favored_team ✅

**Файл:** [analytics/mlb_pitcher_scanner.py:280-305](analytics/mlb_pitcher_scanner.py#L280-L305)

**Проблема:**
T-35 пофиксил trading path (`bot_main._process_pitcher_signal`), но **стандалонный scanner** (`python -m analytics.mlb_pitcher_scanner`) продолжал:
- Брать `matched_market['current_price']` (YES mid) directly
- Вычислять `drift` из YES-side series
- Передавать YES price в `_recommended_action(strength, current_price, hours_to_game)` — решение BUY/WATCH из wrong data
- Писать YES price в `pitcher_signals` rows → dashboard показывает wrong price ~50% markets

Operators (а сам бот опять же) видят signals с misleading context.

**Fix:** scanner вызывает `resolve_team_token_side` и инвертирует:

```python
_, favored_side = await resolve_team_token_side(
    pool, matched_market["id"], favored, aliases
)
if favored_side is None:
    continue   # skip — безопаснее чем показывать wrong price

if favored_side == "YES":
    current_price = yes_mid
    price_24h = yes_24h
else:  # NO — invert
    current_price = 1.0 - yes_mid
    price_24h = (1.0 - yes_24h) if yes_24h is not None else None
```

`favored_side: Optional[str]` добавлен в `PitcherSignal` dataclass для downstream (dashboard) чтобы показывать "NO side" label.

---

## Новые тесты (6)

[tests/test_position_manager_records.py](tests/test_position_manager_records.py) — purely test `_is_buy_rejected`:

| Test | Covers |
|---|---|
| `empty_order_id` | classic rejection, order_id="" |
| `missing_order_id` | dict без order_id key |
| `none_order_id` | py-clob-client иногда None |
| `accepted_order` | real order passes |
| `dry_run_order_is_accepted` | DRY_BUY_* не trigger'ит guard |
| `reason_fallback_when_no_error_key` | reason всегда str, не None |

Пучём regression tests для 3× HIGH #1 call sites достаточно одного helper теста — они all route through `_is_buy_rejected`.

---

## Прогресс по 5 раундам

| Round | Findings | Severity | Fix ticket | Cost |
|---|---|---|---|---|
| 1 | 4 | 3 HIGH + 1 MED | T-37 | ~1h |
| 2 | 3 | 1 HIGH + 2 MED | T-38 | ~1h |
| 3 | 2 | 1 HIGH + 1 MED | T-39 | ~45m |
| 4 | 2 | 1 HIGH + 1 MED | T-40 | ~30m |
| 5 | 3 | 2 HIGH + 1 MED | T-41 | ~50m |
| **Total** | **14** | 8 HIGH + 6 MED | — | **~4h 15m** |

**Тренд:** 4 → 3 → 2 → 2 → 3. Round 5 — первый рост.

**Интерпретация:**
- Round 1-4 focus: runtime crashes, deploy hygiene, semantic accuracy
- Round 5 focus: **edge/rare paths** — rejected orders (never tested with dry_run=true), standalone scanner vs bot, self-updating scripts
- Количество начинает расти когда easy bugs исчерпаны и codex пошёл по subtle flows

**Надо ли round 6?**
- Pro: Round 5 нашёл 3 реальные проблемы → возможно есть ещё
- Con: Round 5 cost = 50 мин. Round 6 likely найдёт improvements не блокеры.
- Budget: каждый round = ~30-60 мин fix + 4 мин review. Affordable.

**Мой совет:** один round 6 final pass. Если 0-2 MED findings без HIGH — ship.

---

## Метаданные

- **Стоимость:** $0 (ChatGPT Plus)
- **Время на review:** ~4 мин
- **Время на T-41 fix:** ~50 мин
- **Кодекс нашёл:** 3 (vs 2, vs 2, vs 3, vs 4)
- **Regression tests added:** 6
- **Total tests:** 118/118 (все pass)
- **Status:** round 6 optional, else ready for `/ship-to-macmini`
