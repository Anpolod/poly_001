"""T-43 regression tests for scripts/db_preflight.py.

Codex round 7 found two preflight bugs:
1. [CRITICAL] `REQUIRED_TABLES` listed `orders`, but `db/schema.sql` creates
   `order_log`. Preflight would fail forever on a clean DB, blocking deploys.
2. [HIGH] Preflight only checked table presence, not column shape. An older
   `open_positions` (missing `side`/`fill_status`/`current_bid`/`exit_order_id`)
   would pass and then crash at the first insert.

These tests pin both invariants so a future rename (or new column added to
the code but not guarded) is caught at test time instead of at deploy time.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import db_preflight  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"


# ─────────────────────────────────────────────────────────────────────────────
# Static contract tests — no DB needed
# ─────────────────────────────────────────────────────────────────────────────


def test_every_required_table_is_created_by_schema_sql() -> None:
    """Round-7 [CRITICAL] regression: every name in REQUIRED_TABLES must
    appear in a `CREATE TABLE IF NOT EXISTS` statement in db/schema.sql.
    This is the test that would have caught `orders` vs `order_log` at
    commit time instead of at first deploy."""
    schema_sql = SCHEMA_PATH.read_text()
    # Match: CREATE TABLE IF NOT EXISTS <name>
    created = set(re.findall(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)",
        schema_sql,
        flags=re.IGNORECASE,
    ))
    missing = [t for t in db_preflight.REQUIRED_TABLES if t not in created]
    assert not missing, (
        f"REQUIRED_TABLES lists names not created by schema.sql: {missing}. "
        f"schema.sql creates: {sorted(created)}"
    )


def test_every_required_column_table_is_also_in_required_tables() -> None:
    """Every table that has REQUIRED_COLUMNS entries must also be in
    REQUIRED_TABLES — otherwise column checks run against tables that
    preflight wouldn't create on a clean DB."""
    missing = [t for t in db_preflight.REQUIRED_COLUMNS
               if t not in db_preflight.REQUIRED_TABLES]
    assert not missing, (
        f"REQUIRED_COLUMNS references tables not in REQUIRED_TABLES: {missing}"
    )


def test_required_tables_covers_all_boot_time_writers() -> None:
    """Round-8 [MED] regression: REQUIRED_TABLES must cover every table that
    a boot-time daemon writes to, not just the subset recently changed.

    Codex round 8 found that `drift_signals` and `spike_signals` were missing
    even though `drift_monitor` and `spike_follow` are enabled by default in
    settings.example.yaml. Preflight would return success against a stale DB,
    and the first real drift/spike event would crash with missing-table errors.

    This test enumerates every `analytics/*` module that either:
      (a) exposes a `persist_*` function imported by trading/bot_main.py, OR
      (b) runs as a standalone daemon via `-m analytics.<module>` in
          scripts/watchdog.sh DAEMON_CMDS / scripts/start_bot.sh
    …then regex-scans that module for `INSERT INTO <table>` and asserts each
    target table is in REQUIRED_TABLES. If someone adds a new scanner that
    writes to a new table, this test fires at commit time.
    """
    bot_src = (REPO_ROOT / "trading" / "bot_main.py").read_text()
    watchdog_src = (REPO_ROOT / "scripts" / "watchdog.sh").read_text()
    startbot_src = (REPO_ROOT / "scripts" / "start_bot.sh").read_text()

    # (a) analytics modules imported by bot_main.py
    imported_modules = set(re.findall(
        r"from\s+analytics\.(\w+)\s+import",
        bot_src,
    ))

    # (b) analytics modules launched as standalone daemons
    #     Matches: `python -m analytics.<module>` in either script.
    daemon_modules = set(re.findall(
        r"analytics\.(\w+)",
        watchdog_src + "\n" + startbot_src,
    ))

    modules_to_scan = imported_modules | daemon_modules
    assert modules_to_scan, "did not find any analytics modules to scan — test logic broken"

    all_writes: dict[str, str] = {}   # table -> module that writes it
    for module in sorted(modules_to_scan):
        module_path = REPO_ROOT / "analytics" / f"{module}.py"
        if not module_path.exists():
            continue
        src = module_path.read_text()
        for target in re.findall(r"INSERT\s+INTO\s+(\w+)", src, flags=re.IGNORECASE):
            all_writes.setdefault(target, module)

    uncovered = [
        (t, src) for t, src in all_writes.items()
        if t not in db_preflight.REQUIRED_TABLES
    ]
    assert not uncovered, (
        "REQUIRED_TABLES missing boot-time writers:\n"
        + "\n".join(f"  - {t}  (written by analytics/{m}.py)" for t, m in uncovered)
        + "\nAdd each missing table to REQUIRED_TABLES in scripts/db_preflight.py."
    )


def test_required_columns_are_present_in_schema_sql() -> None:
    """Every column in REQUIRED_COLUMNS must appear inside the matching
    CREATE TABLE block in schema.sql — so a fresh `apply` always produces
    a satisfying schema."""
    schema_sql = SCHEMA_PATH.read_text()
    for table, columns in db_preflight.REQUIRED_COLUMNS.items():
        # Pull the CREATE TABLE block for this table: everything between
        # "CREATE TABLE IF NOT EXISTS <table> (" and the closing ");".
        match = re.search(
            rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\s*\((.*?)\)\s*;",
            schema_sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        assert match, f"{table} not found as CREATE TABLE in schema.sql"
        block = match.group(1)
        for col in columns:
            # Column names sit at the start of a line inside the block.
            pattern = rf"(^|\s){re.escape(col)}\s+"
            assert re.search(pattern, block), (
                f"Column {col!r} declared required for {table}, but not found "
                f"in its CREATE TABLE block. Block:\n{block}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Behavioural tests — mock the DB connection, run the real main()
# ─────────────────────────────────────────────────────────────────────────────


class _FakeConn:
    """Very small asyncpg.Connection stand-in. Supports fetch() with two
    shapes we care about: the pg_catalog table listing and the
    information_schema.columns listing."""

    def __init__(self, tables: set[str], columns: dict[str, set[str]]):
        self.tables = set(tables)
        self.columns = {k: set(v) for k, v in columns.items()}
        self.applied_schema = False

    async def fetch(self, sql: str, *args):
        if "pg_catalog.pg_tables" in sql:
            return [{"tablename": t} for t in self.tables]
        if "information_schema.columns" in sql:
            filter_list = args[0] if args else list(self.columns.keys())
            out = []
            for t in filter_list:
                for c in self.columns.get(t, ()):
                    out.append({"table_name": t, "column_name": c})
            return out
        raise AssertionError(f"unexpected fetch: {sql[:80]}")

    async def execute(self, sql: str, *args):
        # Simulate applying schema.sql: every CREATE TABLE IF NOT EXISTS
        # introduces the table if not already present. This ensures
        # `_missing_tables` sees the new table on the second check.
        # (Column additions are NOT simulated — that's the whole point of
        # the [HIGH] finding: CREATE TABLE IF NOT EXISTS won't alter.)
        created = re.findall(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)",
            sql,
            flags=re.IGNORECASE,
        )
        for t in created:
            self.tables.add(t)
        self.applied_schema = True

    async def close(self):
        pass

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn
            async def __aexit__(self, *a):
                return None

        return _Tx()


def _run_main(conn: _FakeConn, check_only: bool = False) -> int:
    """Invoke db_preflight.main() with asyncpg.connect patched to return conn."""
    async def fake_connect(**_kwargs):
        return conn

    with patch.object(db_preflight.asyncpg, "connect", new=fake_connect):
        return asyncio.run(db_preflight.main(check_only=check_only))


def _all_required_columns_satisfied() -> dict[str, set[str]]:
    """Helper: return a columns dict that satisfies every REQUIRED_COLUMNS entry."""
    return {t: set(cols) for t, cols in db_preflight.REQUIRED_COLUMNS.items()}


def test_preflight_passes_on_fully_populated_db() -> None:
    """Baseline: all required tables + columns present → exit code 0, no DDL."""
    conn = _FakeConn(
        tables=set(db_preflight.REQUIRED_TABLES),
        columns=_all_required_columns_satisfied(),
    )
    rc = _run_main(conn)
    assert rc == 0
    assert conn.applied_schema is False, "DDL must not run when nothing is missing"


def test_preflight_applies_schema_when_tables_missing() -> None:
    """Round-7 [CRITICAL] regression: if `order_log` was missing on a fresh
    DB, preflight must apply schema.sql once and return 0 — NOT return
    non-zero on a phantom table name (`orders` had this bug)."""
    # Start with an empty DB; _FakeConn.execute() will populate tables from
    # the real schema.sql content.
    conn = _FakeConn(tables=set(), columns=_all_required_columns_satisfied())
    rc = _run_main(conn)
    assert rc == 0
    assert conn.applied_schema is True
    # Every required table must now exist in the fake DB.
    for t in db_preflight.REQUIRED_TABLES:
        assert t in conn.tables, f"preflight did not create {t}"


def test_preflight_fails_on_column_drift_on_open_positions() -> None:
    """Round-7 [HIGH] regression: an older open_positions table lacking the
    new columns must cause a non-zero exit. Previously passed silently."""
    # Simulate a DB that has all tables but open_positions is missing the
    # post-T-35/T-38 columns (`side`, `fill_status`, `current_bid`,
    # `exit_order_id`) — only has the pre-sprint columns.
    columns = _all_required_columns_satisfied()
    columns["open_positions"] = {"id", "market_id"}   # ancient shape
    conn = _FakeConn(
        tables=set(db_preflight.REQUIRED_TABLES),
        columns=columns,
    )
    rc = _run_main(conn)
    assert rc == 4, f"expected exit code 4 (column drift), got {rc}"


def test_preflight_check_mode_never_applies_ddl() -> None:
    """--check must be side-effect-free even when schema drift is detected."""
    conn = _FakeConn(tables=set(), columns=_all_required_columns_satisfied())
    rc = _run_main(conn, check_only=True)
    assert rc == 1
    assert conn.applied_schema is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
