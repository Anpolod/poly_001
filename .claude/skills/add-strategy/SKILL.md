---
name: add-strategy
description: End-to-end recipe for adding a new trading strategy scanner (DB table + analytics module + bot_main integration + config + dry-run test). Use whenever starting a T-## task that adds a new signal source.
argument-hint: "[strategy-name]"
---

# Add Strategy

Use when adding a new signal source that produces tradeable alerts, e.g. a new scanner module like `injury_scanner` or `calibration_signal`.

**Touch order matters.** Always follow: Explore → Schema → Scanner → Config → bot_main → Test.

Skipping ahead creates drift (the schema forgets a column the scanner already uses, or `bot_main` imports something that doesn't exist yet). The two reference implementations — [analytics/injury_scanner.py](analytics/injury_scanner.py) and [analytics/calibration_signal.py](analytics/calibration_signal.py) — both followed this exact order.

---

## 1. Explore existing patterns

Read these before writing any new code — they already solve the common sub-problems:

| Primitive | File | What it gives you |
|---|---|---|
| Rotowire scraping | [analytics/tanking_scanner.py](analytics/tanking_scanner.py) (`check_lineup_news`) | BeautifulSoup session management |
| NBA market lookup | [analytics/tanking_scanner.py](analytics/tanking_scanner.py) (`find_upcoming_nba_markets`) | LATERAL join to latest `price_snapshots` |
| Team-in-question matching | [analytics/tanking_scanner.py](analytics/tanking_scanner.py) (`match_teams_in_question`) | Longest-alias-first substring match |
| DOM walk + abbr map | [analytics/injury_scanner.py](analytics/injury_scanner.py) (`fetch_rotowire_injuries`) | Per-matchup team mapping via `lineup__abbr` |
| Two-mode (build + scan) | [analytics/calibration_signal.py](analytics/calibration_signal.py) | Offline-build edges + online-scan template |
| MLB team matching | [analytics/mlb_pitcher_scanner.py](analytics/mlb_pitcher_scanner.py) | MLB Stats API client + alias map |
| Scanner helper shape | [trading/bot_main.py](trading/bot_main.py) (`_run_injury_scan`, `_run_calibration_scan`) | Internal cadence + Telegram dedupe |

If the task re-uses any of these, **import them — do not copy-paste**.

---

## 2. DB table (`db/schema.sql`)

Add `<strategy>_signals` table. Mirror the shape of existing signal tables:

```sql
-- <Strategy Name> signals
CREATE TABLE IF NOT EXISTS <strategy>_signals (
    id              SERIAL PRIMARY KEY,
    scanned_at      TIMESTAMPTZ DEFAULT NOW(),
    market_id       TEXT NOT NULL,
    game_start      TIMESTAMPTZ,
    -- strategy-specific columns go here
    current_price   FLOAT,
    action          TEXT,           -- BUY / WATCH / SELL / CLOSE
    notes           TEXT,
    traded          BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_<strategy>_signals_market
    ON <strategy>_signals (market_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_<strategy>_signals_game_start
    ON <strategy>_signals (game_start);
```

**Always two indexes** — one for `(market_id, scanned_at DESC)` (dedupe queries), one for `(game_start)` (hours-window queries).

---

## 3. Scanner (`analytics/<strategy>_scanner.py`)

Required shape:

```python
"""<Strategy> Scanner — one-line description.

Usage:
    python -m analytics.<strategy>_scanner
    python -m analytics.<strategy>_scanner --save --dry-run
"""

@dataclass
class <Strategy>Signal:
    market_id: str
    slug: str
    game_start: datetime
    # ... explicit fields, no **kwargs
    action: str            # BUY / WATCH
    notes: str = ""

async def build_<strategy>_signals(
    pool: asyncpg.Pool,
    session: aiohttp.ClientSession,
    ...
) -> list[<Strategy>Signal]:
    ...

async def persist_signals(pool: asyncpg.Pool, signals) -> int:
    # 24h dedupe guard — check (market_id, key_field) in last 24h before insert
    ...

def print_signals(signals) -> None:
    # Human-readable CLI output
    ...

def _parse_args() -> argparse.Namespace:
    ...

def main() -> None:
    ...

if __name__ == "__main__":
    main()
```

**Rules:**
- **Reuse** `find_upcoming_nba_markets` / `find_upcoming_mlb_games` — never duplicate the SQL
- **24h dedupe** in `persist_signals` — skip rows where `(market_id, key_field)` already scanned in last 24h
- **Dataclass, not dict** — explicit fields catch typos at call site
- **Standalone CLI** — every scanner must run as `python -m analytics.<name>` without the bot

---

## 4. Config (`config/settings.yaml` + `settings.example.yaml`)

```yaml
<strategy>:
  enabled: true
  scan_interval_min: <N>
  hours_window: 48
  # strategy-specific thresholds
```

**Always update both files.** `settings.example.yaml` is the source of truth for new installs; forgetting it means the next checkout gets a stale default.

---

## 5. bot_main integration (`trading/bot_main.py`)

Follow the exact shape of `_run_injury_scan` / `_run_calibration_scan`:

```python
# Module-level globals
_last_<strategy>_scan: datetime | None = None
_<strategy>_alerted: dict[str, datetime] = {}  # market_id -> ts

async def _run_<strategy>_scan(
    pool: asyncpg.Pool,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
) -> int:
    global _last_<strategy>_scan

    cfg = config.get("<strategy>", {})
    if not cfg.get("enabled", False):
        return 0

    interval_sec = float(cfg.get("scan_interval_min", 10)) * 60.0
    now = datetime.now(timezone.utc)
    if _last_<strategy>_scan is not None:
        elapsed = (now - _last_<strategy>_scan).total_seconds()
        if elapsed < interval_sec:
            return 0
    _last_<strategy>_scan = now

    try:
        signals = await asyncio.wait_for(
            build_<strategy>_signals(pool, ...),
            timeout=30,
        )
    except asyncio.TimeoutError:
        logger.warning("<strategy> scan timed out")
        return 0
    except Exception as exc:
        logger.warning("<strategy> scan failed: %s", exc)
        return 0

    # ... dedupe via _<strategy>_alerted + Telegram digest
```

**Call site** (inside `run_loop`): wrap in `try/except` so a single bad scan never propagates into the main loop.

**Import the new module** at the top of `bot_main.py`.

**Never auto-execute** on v1 — alert-only. Directional YES/NO mapping is a market-by-market problem; get it reviewed in Telegram first.

---

## 6. Dry-run test

```bash
# Syntax sanity
venv/bin/python -c "import ast; ast.parse(open('analytics/<strategy>_scanner.py').read())"

# Stand-alone CLI
venv/bin/python -m analytics.<strategy>_scanner --dry-run

# Via SSH tunnel against Mac Mini DB
ssh -f -N -L 15432:localhost:5432 mac-mini
# ... run test ...
pkill -f "ssh -f -N -L 15432:localhost:5432"
```

If the scanner scrapes a web page, write a **parse-only test first** (no DB) — it catches DOM drift without needing a live pool.

---

## 7. Close the task

1. Mark `TASKS.md` T-## as ✅ with the date
2. Note in the task entry: "TODO next session on Mac Mini — apply schema, run any one-time build job"
3. Invoke `/finish-task` for the retro
4. Invoke `/ship-to-macmini` at end of session to deploy
