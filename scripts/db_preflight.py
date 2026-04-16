"""Schema preflight — ensure DB is on a compatible version before daemons start.

T-42: a routine code-only deploy via the watchdog can reach the Mac Mini before
anyone has applied `db/schema.sql` manually. Sidecars (export_dashboard_data,
mlb_pitcher_scanner, bot_main itself) then boot against an older schema and
crash continuously. This script runs as step 0 of `start_bot.sh` and:

  1. Connects using the same config path as `db/repository.py`.
  2. Verifies required tables exist — these are the tables added or materially
     changed by the recent sprint (T-28..T-41). If every required table is
     already present, we skip the DDL replay for speed.
  3. If ANY required table is missing, applies `db/schema.sql` in a single
     transaction. The file is idempotent (CREATE TABLE IF NOT EXISTS, CREATE
     INDEX IF NOT EXISTS) so re-applying does not clobber data.
  4. Exits non-zero on any failure — `set -e` in start_bot.sh aborts the
     launch before any daemon that would trip over missing tables.

Run standalone:
    python scripts/db_preflight.py             # verify + apply if needed
    python scripts/db_preflight.py --check     # verify only (no writes)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s preflight: %(message)s",
)
logger = logging.getLogger("db_preflight")

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"

REQUIRED_TABLES = (
    "markets",
    "price_snapshots",
    "trades",
    "open_positions",
    "pitcher_signals",
    "injury_signals",
    "calibration_edges",
    "order_log",       # T-43: audit log of CLOB buys/sells; was mis-named `orders`
    "drift_signals",   # T-44: drift_monitor persists via persist_drift_signals
    "spike_signals",   # T-44: spike_signal persists via persist_spike_signals
    "prop_scan_log",   # T-44: prop_scanner --daemon / --watch writes here
    "tanking_signals", # T-44: tanking_scanner --save writes here
)

# T-43: table-presence alone is not enough — `CREATE TABLE IF NOT EXISTS` will
# never ALTER an existing table, so a deploy onto a DB that has an OLD
# `open_positions` (pre-T-35/T-38) passes the table check but then crashes at
# runtime on `side`, `fill_status`, `current_bid`, `exit_order_id`.
# Keep this set tight: list the columns added/required by the current code,
# not every column in the table. Other tables are new in this sprint, so
# table-existence already covers them.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "open_positions": (
        "side",           # T-38: YES/NO for MLB NO-side favorites
        "fill_status",    # lifecycle: pending|filled|exit_pending|closed|...
        "current_bid",    # stop-loss monitor refreshes
        "exit_order_id",  # T-35: SELL order id; order_poller finalizes
    ),
}


def _load_db_config() -> dict:
    if not SETTINGS_PATH.exists():
        raise SystemExit(f"config not found: {SETTINGS_PATH}")
    with SETTINGS_PATH.open() as f:
        cfg = yaml.safe_load(f)
    db = cfg.get("database", {})
    return {
        "host": os.environ.get("DB_HOST") or db.get("host", "localhost"),
        "port": int(os.environ.get("DB_PORT") or db.get("port", 5432)),
        "database": os.environ.get("DB_NAME") or db.get("name", "polymarket_sports"),
        "user": os.environ.get("DB_USER") or db.get("user", "postgres"),
        "password": os.environ.get("DB_PASSWORD") or str(db.get("password", "")),
    }


async def _missing_tables(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT tablename
        FROM pg_catalog.pg_tables
        WHERE schemaname = 'public'
        """
    )
    existing = {r["tablename"] for r in rows}
    return [t for t in REQUIRED_TABLES if t not in existing]


async def _missing_columns(conn: asyncpg.Connection) -> dict[str, list[str]]:
    """Return {table: [missing_column, ...]} for every table in REQUIRED_COLUMNS
    whose required columns are not all present. Tables absent from the DB are
    skipped here — `_missing_tables()` reports them separately.
    """
    if not REQUIRED_COLUMNS:
        return {}
    rows = await conn.fetch(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ANY($1::text[])
        """,
        list(REQUIRED_COLUMNS.keys()),
    )
    found: dict[str, set[str]] = {}
    for r in rows:
        found.setdefault(r["table_name"], set()).add(r["column_name"])

    gaps: dict[str, list[str]] = {}
    for table, required in REQUIRED_COLUMNS.items():
        if table not in found:
            continue   # table missing — caller handles via _missing_tables
        missing = [c for c in required if c not in found[table]]
        if missing:
            gaps[table] = missing
    return gaps


async def _apply_schema(conn: asyncpg.Connection) -> None:
    if not SCHEMA_PATH.exists():
        raise SystemExit(f"schema file not found: {SCHEMA_PATH}")
    sql = SCHEMA_PATH.read_text()
    async with conn.transaction():
        await conn.execute(sql)


async def main(check_only: bool) -> int:
    db_cfg = _load_db_config()
    try:
        conn = await asyncpg.connect(**db_cfg)
    except Exception as exc:
        logger.error("cannot connect to %s:%s/%s as %s — %s",
                     db_cfg["host"], db_cfg["port"], db_cfg["database"],
                     db_cfg["user"], exc)
        return 2

    try:
        missing = await _missing_tables(conn)
        col_gaps = await _missing_columns(conn)

        if not missing and not col_gaps:
            logger.info(
                "schema OK — %d required tables and all required columns present",
                len(REQUIRED_TABLES),
            )
            return 0

        if missing:
            logger.warning("missing tables: %s", ", ".join(missing))
        for table, cols in col_gaps.items():
            logger.warning("table %s is missing columns: %s", table, ", ".join(cols))

        if check_only:
            logger.error("--check mode: would apply schema.sql, aborting instead")
            return 1

        if missing:
            logger.info("applying %s (idempotent CREATE TABLE IF NOT EXISTS)", SCHEMA_PATH)
            await _apply_schema(conn)
        else:
            logger.info("all tables present — skipping DDL apply, columns still missing")

        still_missing = await _missing_tables(conn)
        still_col_gaps = await _missing_columns(conn)

        if still_missing:
            logger.error("after apply, tables still missing: %s", ", ".join(still_missing))
            return 3

        if still_col_gaps:
            # T-43: CREATE TABLE IF NOT EXISTS does NOT add columns to an
            # existing table. This is a genuine DDL migration the operator
            # must apply by hand (ALTER TABLE ... ADD COLUMN) or by dropping
            # the stale table. Fail loudly so start_bot.sh aborts rather
            # than launching daemons that will crash on the first insert.
            for table, cols in still_col_gaps.items():
                logger.error(
                    "column drift on %s — missing: %s. Apply: ALTER TABLE %s ADD COLUMN %s;",
                    table, ", ".join(cols), table,
                    ", ADD COLUMN ".join(f"{c} TEXT" for c in cols),
                )
            return 4

        logger.info("schema applied — all required tables and columns now present")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify/apply DB schema before daemons launch")
    parser.add_argument("--check", action="store_true",
                        help="verify only, do not write DDL")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(check_only=args.check)))
