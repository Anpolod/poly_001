"""
analytics/movement_analyzer.py

Data quality check and early signal detection from collected price_snapshots.

For each market with >= 10 snapshots, computes:
  - price_range:     max(mid_price) - min(mid_price) over collection window
  - volatility:      stddev(mid_price)
  - avg_spread:      mean spread across snapshots
  - direction:       last_mid - first_mid (positive = drifting up)
  - snapshots_count
  - hours_covered:   span of collection window in hours

Groups by sport, shows top 10 most volatile markets per sport.
Flags markets where price_range > taker_rt_cost/100 as early GO candidates
(price movement already exceeds round-trip cost).

Output: console table + early_movers.csv

Usage:
    python -m analytics.movement_analyzer
    python analytics/movement_analyzer.py
"""

import asyncio
import csv
import logging
import sys
from pathlib import Path

import asyncpg
import yaml

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SQL_MOVEMENTS = """
WITH latest_cost AS (
    -- Most recent taker_rt_cost per market (exclude bad pre-fix data)
    SELECT DISTINCT ON (market_id)
        market_id,
        taker_rt_cost,
        spread_pct
    FROM cost_analysis
    WHERE taker_rt_cost < 50   -- filter out 198.0 artifacts from before fix
    ORDER BY market_id, scanned_at DESC
),
estimated_cost AS (
    -- Fallback: costs computed from latest snapshot for markets not in cost_analysis
    SELECT market_id, taker_rt_cost, spread_pct
    FROM cost_estimates
),
effective_cost AS (
    -- Prefer manual cost_analysis over computed estimates
    SELECT
        m.id AS market_id,
        COALESCE(lc.taker_rt_cost, ec.taker_rt_cost)   AS taker_rt_cost,
        COALESCE(lc.spread_pct,    ec.spread_pct)       AS spread_pct,
        CASE
            WHEN lc.market_id IS NOT NULL THEN 'phase0'
            WHEN ec.market_id IS NOT NULL THEN 'computed'
            ELSE NULL
        END AS cost_source
    FROM markets m
    LEFT JOIN latest_cost lc ON lc.market_id = m.id
    LEFT JOIN estimated_cost ec ON ec.market_id = m.id
),
first_last AS (
    -- First and last mid_price per market for direction
    SELECT DISTINCT ON (market_id)
        market_id,
        FIRST_VALUE(mid_price) OVER w AS first_mid,
        LAST_VALUE(mid_price)  OVER w AS last_mid
    FROM price_snapshots
    WHERE mid_price IS NOT NULL
    WINDOW w AS (PARTITION BY market_id ORDER BY ts
                 ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
    ORDER BY market_id
),
stats AS (
    SELECT
        ps.market_id,
        m.sport,
        m.league,
        m.slug,
        COUNT(*)                                                        AS snapshots_count,
        ROUND(AVG(ps.mid_price)::numeric, 4)                           AS avg_mid,
        ROUND((MAX(ps.mid_price) - MIN(ps.mid_price))::numeric, 4)    AS price_range,
        ROUND(STDDEV(ps.mid_price)::numeric, 4)                        AS volatility,
        ROUND(AVG(ps.spread)::numeric, 4)                              AS avg_spread,
        ROUND(
            EXTRACT(EPOCH FROM (MAX(ps.ts) - MIN(ps.ts))) / 3600.0
        ::numeric, 2)                                                   AS hours_covered,
        MAX(ps.ts)                                                      AS last_ts
    FROM price_snapshots ps
    JOIN markets m ON m.id = ps.market_id
    WHERE ps.mid_price IS NOT NULL
    GROUP BY ps.market_id, m.sport, m.league, m.slug
    HAVING COUNT(*) >= 10
)
SELECT
    s.market_id,
    s.sport,
    s.league,
    s.slug,
    s.snapshots_count,
    s.hours_covered,
    s.avg_mid,
    s.price_range,
    s.volatility,
    s.avg_spread,
    ROUND((fl.last_mid - fl.first_mid)::numeric, 4)  AS direction,
    ec.taker_rt_cost,
    ec.cost_source,
    CASE
        WHEN ec.taker_rt_cost IS NOT NULL
             AND s.price_range > (ec.taker_rt_cost / 100.0)
        THEN 'YES'
        ELSE 'NO'
    END AS early_go,
    CASE
        WHEN ec.taker_rt_cost IS NOT NULL AND ec.taker_rt_cost > 0
        THEN ROUND((s.price_range / (ec.taker_rt_cost / 100.0))::numeric, 2)
        ELSE NULL
    END AS move_cost_ratio
FROM stats s
JOIN first_last fl ON fl.market_id = s.market_id
LEFT JOIN effective_cost ec ON ec.market_id = s.market_id
ORDER BY s.sport, s.volatility DESC NULLS LAST
"""


def _fmt_table(rows: list[dict], headers: list[str]) -> str:
    if not rows:
        return "  (no data)"
    data = [[r.get(h, "") for h in headers] for r in rows]
    if HAS_TABULATE:
        return tabulate(data, headers=headers, tablefmt="simple", floatfmt=".4f")
    # Plain fallback
    col_w = [max(len(str(h)), max((len(str(r.get(h, ""))) for r in rows), default=0))
             for h in headers]
    sep = "  ".join("-" * w for w in col_w)
    header_line = "  ".join(str(h).ljust(w) for h, w in zip(headers, col_w))
    lines = [header_line, sep]
    for r in rows:
        lines.append("  ".join(str(r.get(h, "")).ljust(w) for h, w in zip(headers, col_w)))
    return "\n".join(lines)


async def run(config: dict):
    db = config["database"]
    conn = await asyncpg.connect(
        host=db["host"], port=db["port"], database=db["name"],
        user=db["user"], password=db["password"],
    )

    logger.info("Querying price_snapshots…")
    rows = await conn.fetch(SQL_MOVEMENTS)
    await conn.close()

    if not rows:
        print("No markets with >= 10 snapshots found.")
        return

    all_markets = [dict(r) for r in rows]
    total = len(all_markets)
    early_go = [m for m in all_markets if m["early_go"] == "YES"]

    logger.info(f"Markets analyzed: {total} | Early GO candidates: {len(early_go)}")

    # --- Per-sport top 10 by volatility ---
    sports = sorted({m["sport"] for m in all_markets})
    display_cols = ["slug", "snapshots_count", "hours_covered", "avg_mid",
                    "price_range", "volatility", "avg_spread", "direction",
                    "taker_rt_cost", "move_cost_ratio", "cost_source", "early_go"]

    for sport in sports:
        sport_markets = [m for m in all_markets if m["sport"] == sport]
        top10 = sorted(sport_markets, key=lambda x: x["volatility"] or 0, reverse=True)[:10]

        print(f"\n{'='*100}")
        print(f"  {sport.upper()} — Top {len(top10)} by volatility "
              f"(of {len(sport_markets)} markets with ≥10 snapshots)")
        print(f"{'='*100}")
        print(_fmt_table(top10, display_cols))

    # --- Early GO summary ---
    print(f"\n{'='*100}")
    print(f"  EARLY GO CANDIDATES — price_range > taker_rt_cost  ({len(early_go)} markets)")
    print(f"{'='*100}")
    if early_go:
        early_go_sorted = sorted(early_go, key=lambda x: (x["price_range"] or 0), reverse=True)
        go_cols = ["sport", "slug", "price_range", "volatility", "direction",
                   "taker_rt_cost", "move_cost_ratio", "cost_source", "hours_covered", "snapshots_count"]
        print(_fmt_table(early_go_sorted, go_cols))
    else:
        print("  None yet — price movement has not exceeded round-trip cost.")
        print("  This is expected with <12h of data. Re-run after 24h.")

    # --- Overall stats ---
    print(f"\n{'='*100}")
    print("  OVERALL STATS")
    print(f"{'='*100}")
    total_snaps = sum(m["snapshots_count"] for m in all_markets)
    avg_vol = sum(m["volatility"] or 0 for m in all_markets) / total if total else 0
    avg_hrs = sum(m["hours_covered"] or 0 for m in all_markets) / total if total else 0
    print(f"  Markets analyzed:      {total}")
    print(f"  Total snapshots:       {total_snaps:,}")
    print(f"  Avg volatility:        {avg_vol:.4f}")
    print(f"  Avg hours covered:     {avg_hrs:.1f}h")
    print(f"  Early GO candidates:   {len(early_go)} ({len(early_go)/total*100:.1f}% of analyzed)")

    # --- CSV output ---
    out_path = Path("early_movers.csv")
    csv_cols = ["market_id", "sport", "league", "slug", "snapshots_count", "hours_covered",
                "avg_mid", "price_range", "volatility", "avg_spread", "direction",
                "taker_rt_cost", "move_cost_ratio", "cost_source", "early_go"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(all_markets, key=lambda x: x["volatility"] or 0, reverse=True))

    print(f"\n  CSV written: {out_path} ({len(all_markets)} rows)")


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
