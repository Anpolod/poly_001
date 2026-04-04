"""
analytics/cost_backfill.py

Populates cost_estimates table for markets that have no taker_rt_cost in cost_analysis.

Steps:
  1. Import phase0_results.csv into cost_estimates with source='manual' (skip if already present)
  2. For remaining markets (no cost_analysis row, no manual entry), derive costs from
     the latest price_snapshot (best_bid, best_ask, spread) using the standard formula:
       spread_pct    = spread / best_ask * 100
       taker_rt_cost = (fee_rate * 2 * best_ask * 100) + spread_pct
       maker_rt_cost = spread_pct * 0.5
     fee_rate defaults to 0.0075 when not available in markets table.
  3. Upsert all computed rows into cost_estimates.
  4. Print a summary of coverage.

Usage:
    python -m analytics.cost_backfill
    python analytics/cost_backfill.py
"""

import asyncio
import csv
import logging
import sys
from pathlib import Path

import asyncpg
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_FEE_RATE = 0.0075
PHASE0_CSV = Path("phase0_results.csv")

# SQL: latest snapshot per market for markets lacking a cost_analysis entry
SQL_LATEST_SNAPSHOTS = """
WITH latest AS (
    SELECT DISTINCT ON (ps.market_id)
        ps.market_id,
        ps.best_bid,
        ps.best_ask,
        ps.spread,
        ps.ts,
        COALESCE(m.fee_rate_yes, $1) AS fee_rate
    FROM price_snapshots ps
    JOIN markets m ON m.id = ps.market_id
    WHERE ps.best_bid IS NOT NULL
      AND ps.best_ask IS NOT NULL
      AND ps.best_ask > 0
    ORDER BY ps.market_id, ps.ts DESC
)
SELECT l.*
FROM latest l
WHERE NOT EXISTS (
    SELECT 1 FROM cost_estimates ce
    WHERE ce.market_id = l.market_id
      AND ce.source = 'manual'
)
"""


def _compute_costs(best_bid: float, best_ask: float, spread: float, fee_rate: float) -> dict:
    spread_pct = (spread / best_ask * 100) if best_ask > 0 else 0.0
    taker_rt_cost = (fee_rate * 2 * best_ask * 100) + spread_pct
    maker_rt_cost = spread_pct * 0.5
    return {
        "best_bid": round(best_bid, 4),
        "best_ask": round(best_ask, 4),
        "spread": round(spread, 4),
        "spread_pct": round(spread_pct, 4),
        "taker_rt_cost": round(taker_rt_cost, 4),
        "maker_rt_cost": round(maker_rt_cost, 4),
    }


async def import_phase0_csv(conn) -> int:
    """Load phase0_results.csv into cost_estimates with source='manual'. Skips existing."""
    if not PHASE0_CSV.exists():
        logger.warning(f"{PHASE0_CSV} not found — skipping manual import")
        return 0

    # Verify all market_ids exist in markets table before inserting
    valid_ids: set[str] = {
        row["id"] for row in await conn.fetch("SELECT id FROM markets")
    }

    rows_inserted = 0
    with open(PHASE0_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mid = row.get("market_id", "").strip()
            if not mid or mid not in valid_ids:
                continue
            try:
                best_bid = float(row["best_bid"])
                best_ask = float(row["best_ask"])
                spread = float(row["spread"])
                spread_pct = float(row["spread_pct"])
                taker = float(row["taker_rt_cost"])
                maker = float(row["maker_rt_cost"])
            except (KeyError, ValueError):
                continue

            await conn.execute("""
                INSERT INTO cost_estimates
                    (market_id, best_bid, best_ask, spread, spread_pct,
                     taker_rt_cost, maker_rt_cost, source, computed_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'manual', NOW())
                ON CONFLICT (market_id) DO NOTHING
            """, mid, best_bid, best_ask, spread, spread_pct, taker, maker)
            rows_inserted += 1

    logger.info(f"Manual import from {PHASE0_CSV}: {rows_inserted} rows upserted")
    return rows_inserted


async def backfill_computed(conn) -> tuple[int, int]:
    """Compute costs from latest snapshots for markets without manual entries."""
    rows = await conn.fetch(SQL_LATEST_SNAPSHOTS, DEFAULT_FEE_RATE)
    computed = 0
    skipped = 0

    for row in rows:
        best_bid = float(row["best_bid"])
        best_ask = float(row["best_ask"])
        spread = float(row["spread"]) if row["spread"] is not None else (best_ask - best_bid)
        fee_rate = float(row["fee_rate"])

        costs = _compute_costs(best_bid, best_ask, spread, fee_rate)

        await conn.execute("""
            INSERT INTO cost_estimates
                (market_id, best_bid, best_ask, spread, spread_pct,
                 taker_rt_cost, maker_rt_cost, source, computed_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'computed', NOW())
            ON CONFLICT (market_id) DO UPDATE SET
                best_bid      = EXCLUDED.best_bid,
                best_ask      = EXCLUDED.best_ask,
                spread        = EXCLUDED.spread,
                spread_pct    = EXCLUDED.spread_pct,
                taker_rt_cost = EXCLUDED.taker_rt_cost,
                maker_rt_cost = EXCLUDED.maker_rt_cost,
                computed_at   = NOW()
            WHERE cost_estimates.source = 'computed'
        """,
            row["market_id"],
            costs["best_bid"], costs["best_ask"], costs["spread"], costs["spread_pct"],
            costs["taker_rt_cost"], costs["maker_rt_cost"],
        )
        computed += 1

    return computed, skipped


async def run(config: dict):
    db = config["database"]
    conn = await asyncpg.connect(
        host=db["host"], port=db["port"], database=db["name"],
        user=db["user"], password=db["password"],
    )

    try:
        # Step 1: manual import from CSV
        await import_phase0_csv(conn)

        # Step 2: compute from snapshots
        computed_count, _ = await backfill_computed(conn)

        # Step 3: summary
        total = await conn.fetchval("SELECT COUNT(*) FROM cost_estimates")
        manual_total = await conn.fetchval("SELECT COUNT(*) FROM cost_estimates WHERE source = 'manual'")
        computed_total = await conn.fetchval("SELECT COUNT(*) FROM cost_estimates WHERE source = 'computed'")

        markets_total = await conn.fetchval("SELECT COUNT(DISTINCT market_id) FROM price_snapshots")
        covered = await conn.fetchval("""
            SELECT COUNT(DISTINCT ps.market_id)
            FROM price_snapshots ps
            JOIN cost_estimates ce ON ce.market_id = ps.market_id
        """)
        missing = markets_total - covered

        print(f"\n{'='*60}")
        print("  COST BACKFILL SUMMARY")
        print(f"{'='*60}")
        print(f"  Manual (phase0 CSV):  {manual_total} markets")
        print(f"  Computed (snapshots): {computed_total} markets")
        print(f"  Total in table:       {total} markets")
        print(f"  Markets with data:    {covered}/{markets_total} ({covered/markets_total*100:.1f}%)")
        if missing > 0:
            print(f"  Still missing:        {missing} (no valid snapshots)")
        print(f"{'='*60}")

    finally:
        await conn.close()


def main():
    config_path = Path("config/settings.yaml")
    if not config_path.exists():
        print("ERROR: config/settings.yaml not found")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
