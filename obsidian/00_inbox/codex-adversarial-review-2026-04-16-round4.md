# Codex Adversarial Review — 2026-04-16 Round 4 (+ T-40 fixes)

**Источник:** `/codex:adversarial-review` четвёртый запуск после T-39
**Объём:** 19 modified files, ~1,710 lines
**Thread:** `019d9507-5a70-7a51-ba3b-82e304ee9e4d`
**Verdict round 4:** 🚨 needs-attention — NO-SHIP (1 HIGH + 1 MED)
**Status после T-40 fixes:** ✅ DONE 2026-04-16

---

## TL;DR

Round 4 — 2 findings (те же 2, что и round 3 по количеству). Но качество другое: это не крэши и не state-machine баги, а **operational hygiene** — supervisors не покрывают весь стек, env-vars не распространяются во все процессы.

Обе пофикшены за ~30 мин. Python код не трогался — чисто shell-side.

---

## Финдинги (обе DONE)

### 🔴 [HIGH] #1 — Watchdog supervise'ил только 2 из 6 daemon'ов ✅

**Файл:** [scripts/watchdog.sh:8-106](scripts/watchdog.sh#L8-L106)

**Проблема:**
`start_bot.sh` запускал 6 daemon'ов (bot, collector, dashboard, stats_exporter, stats_server, mlb_scanner), но `watchdog.sh` знал только про bot.pid + collector.pid. Последствия:
- 4 daemon'а не supervised → если crash'нутся, никогда не рестартуются
- `_git_sync` после `git reset --hard` kill'ил только bot + collector → остальные работали stale bytecode indefinitely
- Dashboard показывал старые данные, MLB scanner работал старую логику — silent version skew

**Fix:** рефактор на **array-based supervision**:

```bash
# bash 3.2 compatible (macOS default)
DAEMON_NAMES=(bot collector dashboard stats_exporter stats_server mlb_scanner)
DAEMON_CMDS=(
  "$PYTHON -m trading.bot_main"
  "$PYTHON $REPO/main.py"
  "$PYTHON -m streamlit run $REPO/dashboard/main_dashboard.py ..."
  "$PYTHON $REPO/dashboard/export_dashboard_data.py --watch"
  "$PYTHON -m http.server 8502 --directory $REPO/dashboard"
  "$PYTHON -m analytics.mlb_pitcher_scanner --watch --save"
)
RESTART_COUNTS=(0 0 0 0 0 0)
HEALTHY_TICKS=(0 0 0 0 0 0)

# Main loop
for i in "${!DAEMON_NAMES[@]}"; do
  # one uniform block per daemon — healthy tick bump, reset on stable,
  # restart on dead, MAX_RESTARTS guard
done
```

`_git_sync` теперь итерирует `DAEMON_NAMES[@]` и kill'ит все 6 PID'ов после pull.

---

### 🟡 [MED] #2 — `.env` не loaded для большинства entrypoints ✅

**Файл:** [scripts/start_bot.sh:15-76](scripts/start_bot.sh#L15-L76), [scripts/watchdog.sh](scripts/watchdog.sh)

**Проблема:**
Только `trading/bot_main.py` сам читал `.env`. Остальные Python entrypoints (`main.py`, `export_dashboard_data.py`, `mlb_pitcher_scanner.py`, etc.) полагались на inherited environment. Но shell-скрипты **не source'или** `.env` до spawn'а subprocess'ов → новый `LOCAL_BIND_IP` (macOS interface-selection fix) не доходил до collector'а и sidecars.

Это silent failure — operator добавил LOCAL_BIND_IP в `.env`, думает "ок, fixed", а collector продолжает падать с EADDRNOTAVAIL.

**Fix:** добавил в оба shell-скрипта до spawn'ов:

```bash
if [ -f "$REPO/.env" ]; then
  set -a
  . "$REPO/.env"
  set +a
fi
```

`set -a` автоматически export'ит все переменные, присвоенные из source'нутого файла. `nohup cmd &` наследует env, все 6 daemons теперь получают LOCAL_BIND_IP + POLYGON_PRIVATE_KEY + прочее.

---

## Прогресс по раундам adversarial review (финальный)

| Round | Findings | Severity | Категория | Cost to fix |
|---|---|---|---|---|
| 1 | 4 | 3 HIGH + 1 MED | Runtime crashes (asyncpg.Record `.get()`) | ~1 час (T-37) |
| 2 | 3 | 1 HIGH + 2 MED | Deploy / dry-run mutation / side ledger | ~1 час (T-38) |
| 3 | 2 | 1 HIGH + 1 MED | Dry-run state + watchdog budget | ~45 мин (T-39) |
| 4 | 2 | 1 HIGH + 1 MED | Supervision coverage + env propagation | ~30 мин (T-40) |
| **Total** | **11** | 6 HIGH + 5 MED | — | ~3.25 часа |

**Progression:** 4 → 3 → 2 → 2 findings. Cost падает (1h → 1h → 45min → 30min). Каждый раунд ловит progressively меньше и более subtle проблем.

---

## Надо ли round 5?

**Аргументы за:**
- Round 4 всё ещё NO-SHIP verdict
- Может найти последние observability gaps (нет Telegram alerts на watchdog "max restarts reached", нет metric'ов на deploy rollback)

**Аргументы против:**
- Cost-to-fix тренд: 1h → 45m → 30m. Round 5 likely < 30 min fixes.
- Diminishing returns — следующий раунд скорее всего найдёт improvements а не блокеры
- Мы уже прогнали 4 раунда на 9+ часов реальной работы + ~$0 в API costs

**Мой совет:** один финальный round 5 чтобы убедиться что нет hidden big-block blocker, и если verdict остаётся "needs-attention" с только MED — ship.

---

## Метаданные

- **Стоимость:** $0 (ChatGPT Plus)
- **Время на review:** ~4 мин
- **Время на T-40 fix:** ~30 мин
- **Кодекс нашёл:** 2 (vs 2, vs 3, vs 4)
- **Regression tests added:** 0 (pure shell — no unit test infra for bash supervision)
- **Python tests status:** 112/112 (unchanged — no Python modifications)
- **Status:** round 5 optional, иначе ready to `/ship-to-macmini`
