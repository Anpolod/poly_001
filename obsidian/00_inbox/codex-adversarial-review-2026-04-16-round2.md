# Codex Adversarial Review — 2026-04-16 Round 2

**Источник:** `/codex:adversarial-review` повторный запуск после T-37 fixes
**Объём:** 19 modified files, ~1,600 lines
**Reviewer:** OpenAI Codex
**Тред:** `019d94f0-a8e9-7473-8d84-0b524515883c`
**Verdict: 🚨 needs-attention — NO-SHIP** (но уже по совершенно другим причинам)

---

## TL;DR

Все 4 находки из первого раунда (asyncpg.Record `.get()` bugs + gather silent-failure) **подтверждены как пофикшенные** — codex их не упоминает. Но найдены **3 новые проблемы** другой природы:

1. **Deploy version skew** — watchdog pulls код но не рестартит процессы
2. **Dry-run mutates live state** — `calibration_signal --build --dry-run` пишет в prod DB
3. **NO-side positions сохраняются как YES** — DB ledger врёт для половины MLB сделок

Это **другой класс проблем** — не runtime crashes, а **semantic mismatch** между поведением кода и ожиданиями operator'а/dashboard.

---

## Критические находки

### 🔴 [HIGH] #1 — Watchdog auto-pull обновляет код, но не рестартит процессы

**Файл:** [scripts/watchdog.sh:23-33](scripts/watchdog.sh#L23-L33) (`_git_sync` функция)

**Проблема:**
`_git_sync()` делает `git reset --hard origin/master` — код на диске обновляется. НО потом только `touch dashboard/main_dashboard.py` (чтобы Streamlit авто-перечитал). Логика рестарта watchdog'а ниже в цикле срабатывает **ТОЛЬКО если PID мёртвый** — живые `bot_main` / `main.py` процессы продолжают работать со **старым кодом**.

**Impact — silent version skew:**
- Operator пушит fix → watchdog подхватывает pull → думает "ok, deployed"
- Но bot_main и collector **всё ещё работают на прошлом commit'е**
- Fixing ship-to-macmini skill говорит "проверь логи после push" — этот check пройдёт, но процесс по-прежнему старый
- Hotfix baga не применяется пока что-то не грохнется (может — никогда)

Это особенно острая проблема после T-35 (exit_order_id column, side-aware trading): critical business logic fix не применится без явного рестарта. Operator думает что stop-loss теперь работает, а на деле running process ещё без фикса.

**Fix (варианты):**
- **A)** После успешного `git reset --hard` — убить bot.pid и collector.pid, watchdog их автоматически поднимет на новой версии
- **B)** Сравнивать running commit SHA (из env var или /tmp файла записанного при старте) с текущим HEAD — рестартить если разные
- **C)** Trap-flag после pull → выйти из watchdog → `start_bot.sh` поднимет все заново (грубо, но надёжно)

Рекомендую A — surgical restart через kill PID, полагаясь на уже существующий restart loop.

---

### 🟡 [MEDIUM] #2 — `calibration_signal --build --dry-run` пишет в БД

**Файл:** [analytics/calibration_signal.py:389-400](analytics/calibration_signal.py#L389-L400)

**Проблема:**
CLI help для `--dry-run` говорит **"Print only; no DB writes"**. Но в `_main()`:

```python
if args.build:
    edges = await build_edges(pool, price_col=..., per_market_type=...)   # ← всегда upsert'ит
    print_edges(edges, show_all=args.show_all)
```

`build_edges()` безусловно делает `INSERT ... ON CONFLICT DO UPDATE` в `calibration_edges`. Значит `--build --dry-run` **overwrite'ит live edge table** думая что это preview.

**Impact:**
- Operator хочет посмотреть "что бы получилось если я пересчитаю edges с `--per-market-type`" → запускает `--build --dry-run`
- Ожидает: print + exit
- Реальность: edges пересчитаны и сохранены → **бот на следующем scan cycle использует новую модель**
- Особенно опасно в live trading — одна preview-команда меняет trading behaviour

**Fix:**
Передать `persist: bool` флаг в `build_edges()`:
```python
async def build_edges(pool, ..., persist: bool = True) -> list[CalibrationEdge]:
    ...
    if persist:
        async with pool.acquire() as conn:
            for e in edges:
                await conn.execute("INSERT INTO calibration_edges ...")
    return edges
```

И в `_main`:
```python
if args.build:
    edges = await build_edges(pool, ..., persist=not args.dry_run)
```

Или просто block `--build --dry-run` combo, forcing `--dry-run` сам по себе быть scan-only.

---

### 🟡 [MEDIUM] #3 — NO-side MLB positions записываются как YES в БД

**Файл:** [trading/position_manager.py:20-45](trading/position_manager.py#L20-L45)

**Проблема:**
T-35 дал нам `resolve_team_token_side()` — bot_main теперь корректно покупает NO token когда favored pitcher на NO стороне. Но `open_position()` имеет hardcoded:

```python
INSERT INTO open_positions
    (market_id, slug, signal_type, side, ...)
VALUES ($1,$2,$3,'YES',$4, ...)
                  --^^^^ всегда YES независимо от реального исполнения
```

**Impact:**
- Для ~50% MLB сделок (где favored team на NO стороне) — DB row говорит `side='YES'` а token_id указывает на NO контракт
- Dashboard рисует "YES position at 0.47" когда реально купили NO по цене (1 - 0.47) = 0.53
- P&L расчёты могут быть неправильны если кто-то использует `side` для inversion
- Manual operator review ("зачем бот купил underdog?") увидит противоречие между signal_type=pitcher и side=YES
- T-35 fix был complete code-wise но incomplete ledger-wise

**Fix:**
1. Добавить `side: str` параметр в `open_position()` (default 'YES' для backward compat с tanking/prop которые всегда YES)
2. В `_process_pitcher_signal`: передать `side=favored_side`
3. В `_process_injury_signal` (если когда-нибудь auto-execute): передать `side=healthy_side`
4. Обновить `send_order_confirmation` чтобы показывать реальный side в Telegram

---

## Что ИЗМЕНИЛОСЬ между раундами

| Раунд 1 (до T-37) | Раунд 2 (после T-37) |
|---|---|
| 3 HIGH + 1 MED — все про asyncpg.Record/.get() | ✅ все **подтверждены пофикшены** |
| Runtime crashes при первой открытой позиции | ✅ dict conversion — `.get()` теперь safe везде |
| Collector silent death | ✅ FIRST_EXCEPTION surfaces failures |
| — | 1 HIGH + 2 MED — **новые категории** |
| — | deploy/version skew, dry-run semantics, ledger accuracy |

**Хорошие новости:** мы НЕ регрессили старые баги. Все T-37 фиксы стоят.

**Плохие новости:** adversarial review находит всё более subtle проблемы. Это normal для второго раунда — первый ловит obvious бреши, второй ловит дизайн-проблемы.

---

## Priorities

| # | Severity | Файл | Cost | Blocks live? |
|---|---|---|---|---|
| 1 | 🔴 HIGH | `scripts/watchdog.sh` deploy semantics | ~20 мин | **ДА** (hotfixes не применяются) |
| 2 | 🟡 MED | `calibration_signal.py` dry-run persist flag | ~15 мин | Не блок, но опасная footgun |
| 3 | 🟡 MED | `position_manager.open_position()` side param | ~30 мин | Не блок (dashboard cosmetic bug), но confusing |

**Общий cost:** ~1 час (+ re-review).

---

## Следующий шаг — T-38 ✅ DONE 2026-04-16

Все 3 находки пофикшены за ~1 час:

- [x] ~~`watchdog.sh _git_sync` post-pull restart~~ → kill bot.pid + collector.pid после successful pull, 2s grace
- [x] ~~`calibration_signal --dry-run`~~ → `build_edges(persist=False)` + CLI threading
- [x] ~~`open_position()` side param~~ → default 'YES' (backward compat) + MLB caller passes favored_side + Telegram confirmation shows real side
- [x] Новые тесты (3): default-side contract, NO-side persistence, dry-run no-INSERT proof
- [ ] Round 3 `/codex:adversarial-review` — опционально, для зелёного verdict

**Test status:** 110/110 passing (3 новых + 107 предыдущих).

**Альтернатива — defer:**
- #1 (version skew) можно жить с этим в dry_run эре. Operator всегда может вручную `make stop && make start`. Но когда перейдём на live trading — это блок.
- #2 (dry-run) — easy footgun, стоит пофиксить сейчас
- #3 (side accuracy) — cosmetic пока в dry_run, но critical для post-trade analytics и любого manual review

**Моя рекомендация:** все три сейчас. Overall 1 час работы, и после этого round 3 может дать green verdict → можно shippable push.

---

## Метаданные

- **Стоимость:** $0 (ChatGPT Plus)
- **Время на review:** ~5 минут (codex прочитал 25+ файлов, запускал grep'ы по паттернам)
- **Тред ID:** `019d94f0-a8e9-7473-8d84-0b524515883c`
- **Previous round fixed (T-37):** 4/4 confirmed ✅
- **New findings:** 3 (1 HIGH + 2 MED)
- **Verdict для ship-to-macmini:** Всё ещё NO, но уже не из-за crash risk — semantic accuracy + deploy hygiene
