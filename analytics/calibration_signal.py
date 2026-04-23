"""
Calibration Trader — automate favorite-longshot bias exploitation.

Two modes:

  --build     Offline job. Reads historical_calibration, computes per-bucket
              edge statistics (sport × market_type × price tier), and upserts
              into the calibration_edges table.

  (default)   Online scan. Queries active markets, looks each up against
              calibration_edges, and yields CalibrationSignal objects where
              |edge_pct| >= min_edge_pct and confidence >= min_confidence.

Usage:
    python -m analytics.calibration_signal --build                    # refresh edges
    python -m analytics.calibration_signal --build --show-all         # incl. LOW conf
    python -m analytics.calibration_signal                            # scan active markets
    python -m analytics.calibration_signal --min-edge 3.0 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncpg
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# Fixed price-tier buckets. Must match what build_edges() stores, and what the
# online scan queries with (price_lo <= mid_price < price_hi).
# Ranges are [lo, hi) — the final hi=1.01 guarantees price=1.0 still matches.
_BUCKETS: list[tuple[float, float, str]] = [
    (0.00, 0.30, "heavy dog"),
    (0.30, 0.40, "dog"),
    (0.40, 0.60, "neutral"),
    (0.60, 0.70, "fav"),
    (0.70, 1.01, "heavy fav"),
]

_CONF_HIGH = 50
_CONF_MED = 20
_CONF_LOW = 3

_CONFIDENCE_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


@dataclass
class CalibrationEdge:
    sport: str
    market_type: str
    price_lo: float
    price_hi: float
    avg_price: float
    actual_win_rate: float
    edge_pct: float          # (actual - expected) * 100
    direction: str           # "YES" if underpriced (buy YES), "NO" if overpriced
    n: int
    confidence: str          # HIGH / MEDIUM / LOW


@dataclass
class CalibrationSignal:
    market_id: str
    slug: str
    sport: str
    market_type: str
    current_price: float
    edge: CalibrationEdge
    action: str              # BUY_YES / BUY_NO
    game_start: Optional[datetime]


def _confidence_for(n: int) -> str:
    if n >= _CONF_HIGH:
        return "HIGH"
    if n >= _CONF_MED:
        return "MEDIUM"
    return "LOW"


def _load_config() -> dict:
    with (_ROOT / "config" / "settings.yaml").open() as f:
        return yaml.safe_load(f)


async def _create_pool(config: dict) -> asyncpg.Pool:
    db = config["database"]
    return await asyncpg.create_pool(
        host=db["host"], port=db["port"], database=db["name"],
        user=db["user"], password=str(db["password"]),
        min_size=1, max_size=3,
    )


# ---------------------------------------------------------------------------
# Build mode — aggregate historical_calibration → calibration_edges
# ---------------------------------------------------------------------------


async def build_edges(
    pool: asyncpg.Pool,
    price_col: str = "price_close",
    per_market_type: bool = False,
    persist: bool = True,
) -> list[CalibrationEdge]:
    """Compute edges per (sport, market_type, price_tier) from historical data.

    Returns the freshly-computed list. When `persist=True` (default) also
    upserts into `calibration_edges`. T-38: `--dry-run` from the CLI sets
    persist=False so a preview never mutates the live trading model.
    If `per_market_type` is False, aggregates across market_types (market_type='any').
    """
    async with pool.acquire() as conn:
        # Sanity check the table exists and has enough data
        try:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM historical_calibration WHERE {price_col} IS NOT NULL AND outcome IS NOT NULL"
            )
        except asyncpg.exceptions.UndefinedTableError:
            logger.error("historical_calibration table does not exist. Run: "
                         "python -m db.init_schema, then python -m analytics.historical_fetcher")
            return []

        if total < _CONF_LOW:
            logger.warning("historical_calibration has only %d usable rows — need more data", total)
            return []

        logger.info("Building edges from %s usable historical rows", f"{total:,}")

        # Pull all rows, we'll bucket in Python since sample counts are small.
        rows = await conn.fetch(
            f"""
            SELECT sport,
                   COALESCE(market_type, 'unknown') AS market_type,
                   {price_col}::float AS price,
                   outcome
            FROM historical_calibration
            WHERE {price_col} IS NOT NULL
              AND outcome IS NOT NULL
              AND sport IS NOT NULL
            """
        )

    # Aggregate into buckets in-memory
    # key: (sport, market_type_key, bucket_lo) -> list[(price, outcome)]
    agg: dict[tuple[str, str, float], list[tuple[float, int]]] = {}
    for r in rows:
        sport = r["sport"]
        mt = r["market_type"] if per_market_type else "any"
        price = float(r["price"])
        outcome = int(r["outcome"])
        for lo, hi, _label in _BUCKETS:
            if lo <= price < hi:
                agg.setdefault((sport, mt, lo), []).append((price, outcome))
                break

    edges: list[CalibrationEdge] = []
    for (sport, mt, lo), items in agg.items():
        n = len(items)
        if n < _CONF_LOW:
            continue
        hi = next(h for low, h, _ in _BUCKETS if low == lo)
        avg_price = sum(p for p, _ in items) / n
        actual_win_rate = sum(o for _, o in items) / n
        edge_pct = (actual_win_rate - avg_price) * 100
        direction = "YES" if edge_pct >= 0 else "NO"
        confidence = _confidence_for(n)

        edges.append(CalibrationEdge(
            sport=sport,
            market_type=mt,
            price_lo=round(lo, 4),
            price_hi=round(hi, 4),
            avg_price=round(avg_price, 4),
            actual_win_rate=round(actual_win_rate, 4),
            edge_pct=round(edge_pct, 3),
            direction=direction,
            n=n,
            confidence=confidence,
        ))

    # T-38: honour the `persist` flag — `--dry-run` must not mutate the live
    # edge table, even though it still needs the computed edges for printing.
    if not persist:
        logger.info("build_edges: persist=False — skipping calibration_edges upsert")
        return edges

    async with pool.acquire() as conn:
        for e in edges:
            await conn.execute(
                """
                INSERT INTO calibration_edges
                    (sport, market_type, price_lo, price_hi,
                     avg_price, actual_win_rate, edge_pct, direction,
                     n, confidence, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, NOW())
                ON CONFLICT (sport, market_type, price_lo) DO UPDATE SET
                    price_hi = EXCLUDED.price_hi,
                    avg_price = EXCLUDED.avg_price,
                    actual_win_rate = EXCLUDED.actual_win_rate,
                    edge_pct = EXCLUDED.edge_pct,
                    direction = EXCLUDED.direction,
                    n = EXCLUDED.n,
                    confidence = EXCLUDED.confidence,
                    updated_at = NOW()
                """,
                e.sport, e.market_type, e.price_lo, e.price_hi,
                e.avg_price, e.actual_win_rate, e.edge_pct, e.direction,
                e.n, e.confidence,
            )
    return edges


def print_edges(edges: list[CalibrationEdge], show_all: bool = False) -> None:
    if not edges:
        print("\nNo edges computed (insufficient historical data).\n")
        return
    # Sort: confidence desc, then |edge_pct| desc
    rows = sorted(
        edges,
        key=lambda e: (-_CONFIDENCE_RANK[e.confidence], -abs(e.edge_pct)),
    )
    if not show_all:
        rows = [e for e in rows if e.confidence in ("HIGH", "MEDIUM")]

    print("\n" + "=" * 100)
    print(f"  CALIBRATION EDGES  —  {len(rows)} rows  (updated {datetime.now(tz=timezone.utc):%Y-%m-%d %H:%M UTC})")
    print("=" * 100)
    print(f"  {'sport':14s} {'mtype':12s} {'range':14s} {'avg':>6s} {'actual':>8s} "
          f"{'edge%':>8s} {'dir':>4s} {'n':>6s} {'conf':>6s}")
    print("  " + "-" * 96)
    for e in rows:
        range_str = f"{e.price_lo:.2f}-{e.price_hi:.2f}"
        print(f"  {e.sport[:14]:14s} {e.market_type[:12]:12s} {range_str:14s} "
              f"{e.avg_price:6.3f} {e.actual_win_rate:8.3f} "
              f"{e.edge_pct:+8.2f} {e.direction:>4s} {e.n:6d} {e.confidence:>6s}")
    print("=" * 100 + "\n")


# ---------------------------------------------------------------------------
# Scan mode — match active markets against calibration_edges
# ---------------------------------------------------------------------------


async def scan(
    pool: asyncpg.Pool,
    min_edge_pct: float = 5.0,
    min_confidence: str = "HIGH",
    hours_window: float = 48.0,
    max_signals: int = 20,
) -> list[CalibrationSignal]:
    """Query active markets with a recent price_snapshot, match against calibration_edges.

    Only returns signals where |edge_pct| >= min_edge_pct AND confidence rank
    satisfies min_confidence. Sorted by |edge_pct| descending.
    """
    min_rank = _CONFIDENCE_RANK.get(min_confidence.upper(), 3)

    async with pool.acquire() as conn:
        # Check calibration_edges exists and has data
        try:
            edge_count = await conn.fetchval("SELECT COUNT(*) FROM calibration_edges")
        except asyncpg.exceptions.UndefinedTableError:
            logger.warning("calibration_edges table missing — run `--build` first")
            return []
        if not edge_count:
            logger.warning("calibration_edges is empty — run `--build` first")
            return []

        # Pull active markets with a recent snapshot. LATERAL join gets the
        # freshest mid_price per market in one query.
        rows = await conn.fetch(
            """
            SELECT
                m.id, m.slug, m.question, m.sport, m.event_start,
                latest.mid_price::float AS current_price
            FROM markets m
            JOIN LATERAL (
                SELECT mid_price
                FROM price_snapshots
                WHERE market_id = m.id
                ORDER BY ts DESC
                LIMIT 1
            ) latest ON TRUE
            WHERE m.status = 'active'
              AND m.event_start BETWEEN NOW() AND NOW() + ($1 * INTERVAL '1 hour')
              AND latest.mid_price IS NOT NULL
              AND m.sport IS NOT NULL
            """,
            float(hours_window),
        )

    if not rows:
        return []

    # Fetch edges once — small table, cheap to hold in memory
    async with pool.acquire() as conn:
        edge_rows = await conn.fetch(
            """
            SELECT sport, market_type, price_lo, price_hi,
                   avg_price, actual_win_rate, edge_pct,
                   direction, n, confidence
            FROM calibration_edges
            """
        )

    edges_by_sport: dict[str, list[CalibrationEdge]] = {}
    for er in edge_rows:
        e = CalibrationEdge(
            sport=er["sport"],
            market_type=er["market_type"],
            price_lo=float(er["price_lo"]),
            price_hi=float(er["price_hi"]),
            avg_price=float(er["avg_price"]),
            actual_win_rate=float(er["actual_win_rate"]),
            edge_pct=float(er["edge_pct"]),
            direction=er["direction"],
            n=int(er["n"]),
            confidence=er["confidence"],
        )
        edges_by_sport.setdefault(e.sport, []).append(e)

    signals: list[CalibrationSignal] = []
    for r in rows:
        sport = r["sport"]
        price = float(r["current_price"])
        candidates = edges_by_sport.get(sport, [])
        # Match only `market_type='any'` edges in scan mode. The `markets` table
        # has no market_type column today, so we cannot safely match
        # per-market-type edges to active markets — picking a moneyline edge for
        # a totals market on price alone would produce false signals.
        # Once markets.market_type is populated, this can become a real filter.
        match: Optional[CalibrationEdge] = None
        for e in candidates:
            if e.market_type != "any":
                continue
            if e.price_lo <= price < e.price_hi:
                match = e
                break
        if match is None:
            continue
        if _CONFIDENCE_RANK[match.confidence] < min_rank:
            continue
        if abs(match.edge_pct) < min_edge_pct:
            continue

        action = "BUY_YES" if match.direction == "YES" else "BUY_NO"
        signals.append(CalibrationSignal(
            market_id=r["id"],
            slug=r["slug"] or r["id"],
            sport=sport,
            market_type=match.market_type,
            current_price=round(price, 4),
            edge=match,
            action=action,
            game_start=r["event_start"],
        ))

    signals.sort(key=lambda s: -abs(s.edge.edge_pct))
    return signals[:max_signals]


def print_signals(signals: list[CalibrationSignal]) -> None:
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("\n" + "=" * 100)
    print(f"  CALIBRATION TRADER SCAN  —  {now_str}")
    print("=" * 100)
    if not signals:
        print("\n  No calibration signals matched the current filter.\n")
        print("=" * 100 + "\n")
        return
    for s in signals:
        print(f"\n  [{s.action}]  {s.slug}")
        print(f"    sport={s.sport}  price={s.current_price:.3f}  "
              f"edge={s.edge.edge_pct:+.2f}%  n={s.edge.n}  conf={s.edge.confidence}")
        if s.game_start:
            print(f"    game: {s.game_start.strftime('%Y-%m-%d %H:%M UTC')}")
    print("\n" + "=" * 100 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _main(args: argparse.Namespace) -> None:
    config = _load_config()
    pool = await _create_pool(config)
    try:
        if args.build:
            # T-38: `--dry-run` makes the whole build non-mutating.
            edges = await build_edges(
                pool,
                price_col=args.price_col,
                per_market_type=args.per_market_type,
                persist=not args.dry_run,
            )
            print_edges(edges, show_all=args.show_all)
            if args.dry_run:
                logger.info("DRY-RUN: computed %d edge rows — NOT persisted", len(edges))
            else:
                logger.info("Built and persisted %d edge rows", len(edges))
        else:
            signals = await scan(
                pool,
                min_edge_pct=args.min_edge,
                min_confidence=args.min_confidence,
                hours_window=args.hours,
                max_signals=args.max_signals,
            )
            print_signals(signals)
    finally:
        await pool.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Calibration Trader — favorite-longshot bias edge")
    p.add_argument("--build", action="store_true",
                   help="Rebuild calibration_edges from historical_calibration")
    p.add_argument("--price-col", default="price_close",
                   choices=["price_close", "price_pre1h", "price_pre2h",
                            "price_pre6h", "price_pre12h", "price_pre24h"],
                   help="Which pre-game price to use when building edges")
    p.add_argument("--per-market-type", action="store_true",
                   help="Build separate edges per market_type (default aggregates to 'any')")
    p.add_argument("--show-all", action="store_true",
                   help="Show LOW-confidence rows when printing edges")
    p.add_argument("--min-edge", type=float, default=5.0,
                   help="Minimum |edge_pct| to emit a signal (default 5.0)")
    p.add_argument("--min-confidence", default="HIGH",
                   choices=["HIGH", "MEDIUM", "LOW"],
                   help="Minimum edge confidence (default HIGH)")
    p.add_argument("--hours", type=float, default=48.0,
                   help="Scan games starting within N hours (default 48)")
    p.add_argument("--max-signals", type=int, default=20,
                   help="Cap the number of returned signals (default 20)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print only; no DB writes (build-mode only)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
