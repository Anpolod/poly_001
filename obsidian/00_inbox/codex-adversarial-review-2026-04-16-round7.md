# Codex Adversarial Review — 2026-04-16 Round 7 (+ T-43 fixes)

**Источник:** `/codex:adversarial-review` седьмой запуск после T-42
**Объём:** 19 modified + несколько untracked files, ~1,961 lines
**Thread:** `019d960b-ec5d-7ac1-8249-ccf969cef51d`
**Verdict round 7:** 🚨 needs-attention — NO-SHIP (1 CRITICAL + 1 HIGH)
**Status после T-43 fixes:** ✅ DONE 2026-04-16

---

## TL;DR

Round 7 нашёл **2 находки — и обе в коде который сам T-42 и добавил**. Т.е. preflight, который мы построили именно чтобы закрыть deploy-hygiene gap, сам был broken out of the box. Классический "who watches the watchmen".

Оба пофикшены за ~25 мин. Добавлен static test который parsит `db/schema.sql` regex'ом и assert'ит что каждое имя из `REQUIRED_TABLES` реально создаётся — этот тест поймал бы баг #1 at commit time.

---

## Найденные проблемы

### 🔴 [CRITICAL] #1 — Preflight никогда не проходил на чистой БД ✅

**Файл:** [scripts/db_preflight.py:43-52](scripts/db_preflight.py#L43-L52)

**Проблема:**
`start_bot.sh` теперь hard-block'ит запуск daemon'ов на успехе `db_preflight.py`. Но `REQUIRED_TABLES` содержал `"orders"`, а `db/schema.sql` создаёт таблицу **`order_log`** — не `orders`:

```python
REQUIRED_TABLES = (
    "markets",
    ...,
    "orders",   # ← НЕ СУЩЕСТВУЕТ в schema.sql!
)
```

Последовательность на чистой БД:
1. `_missing_tables()` → `["orders", ...]`
2. `_apply_schema()` → прогоняет `schema.sql` (создаёт `order_log`, но **не** `orders`)
3. `_missing_tables()` после apply → всё равно `["orders"]`
4. `return 3`
5. `start_bot.sh` `exit 1` → бот никогда не стартует

**Это не corner case — это deterministic deploy blocker.** T-42 было бы невозможно развернуть на свежий Mac Mini. Ирония: preflight, который должен был закрыть deploy hygiene gap, сам был deploy blocker.

**Fix:**
```python
REQUIRED_TABLES = (
    ...,
    "order_log",  # T-43: audit log of CLOB buys/sells; был mis-named `orders`
)
```

+ новый static test который рисует linting trip-wire:

```python
def test_every_required_table_is_created_by_schema_sql() -> None:
    schema_sql = SCHEMA_PATH.read_text()
    created = set(re.findall(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)",
        schema_sql, flags=re.IGNORECASE,
    ))
    missing = [t for t in db_preflight.REQUIRED_TABLES if t not in created]
    assert not missing, (
        f"REQUIRED_TABLES lists names not created by schema.sql: {missing}"
    )
```

Теперь любой rename или добавление в REQUIRED_TABLES без matching `CREATE TABLE IF NOT EXISTS` **падает at test time, не deploy time**. ~30 мс, pure static, работает на любой CI.

### 🟠 [HIGH] #2 — Preflight проверял только таблицы, не columns ✅

**Файл:** [scripts/db_preflight.py:72-81](scripts/db_preflight.py#L72-L81)

**Проблема:**
`_missing_tables()` inspectит только `pg_catalog.pg_tables`. **Не видит column drift.**

Критично потому что:
- `CREATE TABLE IF NOT EXISTS` **не делает ALTER** на existing table — it's a no-op если table уже exists с любой shape
- T-35/T-38 добавили новые columns на `open_positions`: `side`, `fill_status`, `current_bid`, `exit_order_id`
- Если production DB уже имеет `open_positions` (pre-sprint shape), preflight passes → бот стартует → первый `INSERT INTO open_positions (..., side, ...)` падает с undefined-column error

Последствия:
1. Watchdog рестартит падающий bot → crashloop
2. HEALTHY_RESET_TICKS не успевает reset restart counter
3. MAX_RESTARTS=10 → watchdog gives up → supervision outage
4. Нужно manual ALTER TABLE + manual restart

**Fix:** добавил column-level check через `information_schema.columns`:

```python
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "open_positions": (
        "side",           # T-38: YES/NO для MLB NO-side favorites
        "fill_status",    # lifecycle: pending|filled|exit_pending|closed|...
        "current_bid",    # stop-loss monitor refreshes
        "exit_order_id",  # T-35: SELL order id; order_poller finalizes
    ),
}

async def _missing_columns(conn) -> dict[str, list[str]]:
    rows = await conn.fetch("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ANY($1::text[])
    """, list(REQUIRED_COLUMNS.keys()))
    # ... build gaps dict ...
```

Новый exit code **4** с чётким remediation hint:

```
ERROR: column drift on open_positions — missing: side, fill_status.
Apply: ALTER TABLE open_positions ADD COLUMN side TEXT, ADD COLUMN fill_status TEXT;
```

**Выбор — не auto-ALTER production:** deliberate line. `CREATE TABLE IF NOT EXISTS` безопасен, а `ALTER TABLE ADD COLUMN` — нет (lock behavior на больших таблицах, type inference risks). Fail fast + human review лучше чем auto-mutate production.

---

## Новые тесты (7)

[tests/test_db_preflight.py](tests/test_db_preflight.py) — новый файл, static + behavioural:

| Test | Covers |
|---|---|
| `test_every_required_table_is_created_by_schema_sql` | **Static — ловит #1 at commit time** (regex по schema.sql) |
| `test_every_required_column_table_is_also_in_required_tables` | REQUIRED_COLUMNS без REQUIRED_TABLES entry = useless |
| `test_required_columns_are_present_in_schema_sql` | Каждый required column присутствует в schema.sql |
| `test_preflight_passes_on_fully_populated_db` | Baseline: no DDL run когда ничего не missing |
| `test_preflight_applies_schema_when_tables_missing` | Regression #1: empty DB → apply + return 0 |
| `test_preflight_fails_on_column_drift_on_open_positions` | Regression #2: exit code 4 когда columns drift |
| `test_preflight_check_mode_never_applies_ddl` | `--check` side-effect-free |

**`_FakeConn`** — малый asyncpg stand-in. Важно: он **simulates** `CREATE TABLE IF NOT EXISTS`, но **НЕ** column additions. Это и есть суть #2 — re-применение schema.sql не добавляет columns в existing table.

---

## Прогресс по 7 раундам

| Round | Findings | Severity | Fix ticket | Cost |
|---|---|---|---|---|
| 1 | 4 | 3 HIGH + 1 MED | T-37 | ~1h |
| 2 | 3 | 1 HIGH + 2 MED | T-38 | ~1h |
| 3 | 2 | 1 HIGH + 1 MED | T-39 | ~45m |
| 4 | 2 | 1 HIGH + 1 MED | T-40 | ~30m |
| 5 | 3 | 2 HIGH + 1 MED | T-41 | ~50m |
| 6 | 3 | 2 HIGH + 1 MED | T-42 | ~40m |
| 7 | 2 | **1 CRIT** + 1 HIGH | T-43 | ~25m |
| **Total** | **19** | 1 CRIT + 11 HIGH + 7 MED | — | **~5h 30m** |

**Тренд находок:** 4 → 3 → 2 → 2 → 3 → 3 → 2. Не монотонный, но общий уровень падает.

**Интерпретация round 7:**
- **Это первый CRITICAL** за все 7 раундов. И он нашёлся в коде, который сам round 6 и добавил.
- Round 7 focus: **correctness of previous fixes themselves**. Codex заметил что T-42 preflight ссылается на таблицу которой нет в schema.
- Meta-lesson: fix-of-fix bugs реальны. Каждый новый layer ships untested against the real artifact it guards, если ты не пишешь bridge test (в нашем случае — regex parser schema.sql).

**Надо ли round 8?**
- **Pro:** trend не convergent — каждый раунд находил что-то. Round 7 specifically поймал bug в коде T-42. Round 8 может поймать bug в коде T-43 (new regex parser, new exit code, new `information_schema.columns` query).
- **Con:** diminishing returns. 19 findings across 7 rounds = ~2.7/round. Stakes на каждый round = ~30-60 мин.
- **Budget:** cheap (~4 мин review + possible fix)

**Мой совет:** один round 8 final pass. Если 0 findings — ship. Если ещё один fix-of-fix — **значит тренд "каждый round находит что-то новое" персистентный и мы должны смириться что адверсарный review не сходится к нулю** — тогда просто ship на принципе best-effort.

---

## Заметка по round 6

Round 6 не получил отдельного obsidian-файла (я пропустил). Его находки + fix задокументированы в [TASKS.md T-42](../TASKS.md). TL;DR round 6:
- HIGH: double-inversion цены NO-side MLB (scanner изменил контракт `current_price` в T-41, execution path flipped второй раз)
- HIGH: start_bot.sh launched daemons без schema preflight (deploy hygiene gap)
- MED: Telegram order confirmation всегда рендерил "YES @ price" даже для NO-side fills

Все 3 исправлены в T-42. Round 7 именно потому что preflight был introduce'н в T-42, и содержал свои баги.

---

## Метаданные

- **Стоимость:** $0 (ChatGPT Plus)
- **Время на review:** ~4 мин
- **Время на T-43 fix:** ~25 мин
- **Кодекс нашёл:** 2 (vs 3, vs 3, vs 2, vs 2, vs 3, vs 4)
- **Regression tests added:** 7
- **Total tests:** 214/214 (все pass)
- **Status:** round 8 optional, else ready for `/ship-to-macmini`
