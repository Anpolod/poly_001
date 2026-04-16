"""
Cross-sport Real-time Drift Monitor (T-30)

Flags markets that have drifted >= drift_threshold over the lookback window
(default 6h). Cross-references with spike_events to suppress noisy candidates
where a microstructure spike already explained the move.

Alert-only: drift alone is not a tradeable edge. The point is to surface
"something is happening here" so a human can check news / scoreboards.

Usage:
    python -m analytics.drift_monitor                       # default 6h, 4%
    python -m analytics.drift_monitor --threshold 5.0
    python -m analytics.drift_monitor --hours 12
    python -m analytics.drift_monitor --sport basketball
    python -m analytics.drift_monitor --save                # persist to drift_signals
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


@dataclass
class DriftSignal:
    market_id: str
    slug: str
    sport: str
    game_start: Optional[datetime]
    current_price: float
    past_price: float
    lookback_hours: float
    drift_pct: float          # signed, percentage points
    direction: str            # UP / DOWN
    has_spike: bool
    action: str               # WATCH
    notes: str = ""


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


async def scan(
    pool: asyncpg.Pool,
    drift_threshold_pct: float = 4.0,
    lookback_hours: float = 6.0,
    upcoming_hours: float = 48.0,
    sport_filter: Optional[str] = None,
    max_signals: int = 50,
) -> list[DriftSignal]:
    """Find markets that drifted >= drift_threshold_pct over the last `lookback_hours`.

    Cross-checks `spike_events` to flag (not suppress) candidates where a spike
    landed inside the lookback window — caller decides whether to ignore.
    """
    sport_clause = ""
    params: list = [float(lookback_hours), float(upcoming_hours)]
    if sport_filter:
        sport_clause = "AND m.sport ILIKE $3"
        params.append(f"%{sport_filter}%")

    query = f"""
        SELECT
            m.id, m.slug, m.sport, m.event_start,
            latest.mid_price::float AS current_price,
            latest.ts                AS latest_ts,
            past.mid_price::float    AS past_price,
            past.ts                  AS past_ts
        FROM markets m
        JOIN LATERAL (
            SELECT mid_price, ts
            FROM price_snapshots
            WHERE market_id = m.id
            ORDER BY ts DESC
            LIMIT 1
        ) latest ON TRUE
        JOIN LATERAL (
            -- Snapshot closest to (now - lookback_hours), ±30 min tolerance
            SELECT mid_price, ts
            FROM price_snapshots
            WHERE market_id = m.id
              AND ts BETWEEN NOW() - ($1 * INTERVAL '1 hour') - INTERVAL '30 minutes'
                         AND NOW() - ($1 * INTERVAL '1 hour') + INTERVAL '30 minutes'
            ORDER BY ABS(EXTRACT(EPOCH FROM (ts - (NOW() - ($1 * INTERVAL '1 hour')))))
            LIMIT 1
        ) past ON TRUE
        WHERE m.status = 'active'
          AND m.event_start BETWEEN NOW() AND NOW() + ($2 * INTERVAL '1 hour')
          AND latest.mid_price IS NOT NULL
          AND past.mid_price   IS NOT NULL
          AND past.mid_price   > 0
          {sport_clause}
        ORDER BY m.event_start
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    if not rows:
        return []

    signals: list[DriftSignal] = []
    for r in rows:
        current = float(r["current_price"])
        past = float(r["past_price"])
        if past <= 0:
            continue
        drift_pct = (current - past) / past * 100.0
        if abs(drift_pct) < drift_threshold_pct:
            continue

        # Check whether a spike landed inside the lookback window for this market.
        # spike_events is small and indexed on (market_id, start_ts) — cheap.
        async with pool.acquire() as conn:
            spike_row = await conn.fetchrow(
                """
                SELECT id, magnitude, direction
                FROM spike_events
                WHERE market_id = $1
                  AND start_ts >= NOW() - ($2 * INTERVAL '1 hour')
                ORDER BY start_ts DESC
                LIMIT 1
                """,
                r["id"], float(lookback_hours),
            )

        has_spike = spike_row is not None
        notes = ""
        if has_spike:
            notes = (
                f"spike id={spike_row['id']} mag={float(spike_row['magnitude']):.4f} "
                f"dir={spike_row['direction']}"
            )

        event_start = r["event_start"]
        if event_start and event_start.tzinfo is None:
            event_start = event_start.replace(tzinfo=timezone.utc)

        signals.append(DriftSignal(
            market_id=r["id"],
            slug=r["slug"] or r["id"],
            sport=r["sport"] or "other",
            game_start=event_start,
            current_price=round(current, 4),
            past_price=round(past, 4),
            lookback_hours=lookback_hours,
            drift_pct=round(drift_pct, 3),
            direction="UP" if drift_pct > 0 else "DOWN",
            has_spike=has_spike,
            action="WATCH",
            notes=notes,
        ))

    # Sort: largest absolute drift first, but push has_spike candidates lower
    signals.sort(key=lambda s: (s.has_spike, -abs(s.drift_pct)))
    return signals[:max_signals]


async def persist_signals(pool: asyncpg.Pool, signals: list[DriftSignal]) -> int:
    """Insert into drift_signals; skip dupes (same market within last 1h)."""
    inserted = 0
    for s in signals:
        exists = await pool.fetchval(
            """
            SELECT 1 FROM drift_signals
            WHERE market_id = $1
              AND scanned_at > NOW() - INTERVAL '1 hour'
            LIMIT 1
            """,
            s.market_id,
        )
        if exists:
            continue
        await pool.execute(
            """
            INSERT INTO drift_signals
                (market_id, sport, game_start, current_price, past_price,
                 lookback_hours, drift_pct, direction, has_spike, action, notes)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
            s.market_id, s.sport, s.game_start, s.current_price, s.past_price,
            s.lookback_hours, s.drift_pct, s.direction, s.has_spike, s.action, s.notes,
        )
        inserted += 1
    return inserted


def print_signals(signals: list[DriftSignal]) -> None:
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("\n" + "=" * 100)
    print(f"  DRIFT MONITOR — {now_str}")
    print("=" * 100)
    if not signals:
        print("\n  No drift signals matched the threshold.\n")
        print("=" * 100 + "\n")
        return

    print(f"  {'sport':12s}  {'past':>6s} -> {'now':>6s}  {'drift%':>8s} {'dir':>5s} "
          f"{'spike':>6s}  slug")
    print("  " + "-" * 96)
    for s in signals:
        spike_flag = "⚠" if s.has_spike else " "
        print(f"  {s.sport[:12]:12s}  {s.past_price:6.3f} -> {s.current_price:6.3f}  "
              f"{s.drift_pct:+8.2f} {s.direction:>5s}  {spike_flag:>6s}  {s.slug[:50]}")
        if s.notes:
            print(f"  {'':12s}    {s.notes}")
    print("\n" + "=" * 100 + "\n")


async def _main(args: argparse.Namespace) -> None:
    config = _load_config()
    pool = await _create_pool(config)
    try:
        signals = await scan(
            pool,
            drift_threshold_pct=args.threshold,
            lookback_hours=args.hours,
            upcoming_hours=args.upcoming_hours,
            sport_filter=args.sport,
            max_signals=args.max_signals,
        )
        print_signals(signals)
        if args.save and not args.dry_run and signals:
            n = await persist_signals(pool, signals)
            print(f"  Saved {n} new drift_signals row(s).\n")
    finally:
        await pool.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cross-sport real-time drift monitor")
    p.add_argument("--threshold", type=float, default=4.0,
                   help="Minimum |drift_pct| to alert (default 4.0)")
    p.add_argument("--hours", type=float, default=6.0,
                   help="Lookback window in hours (default 6.0)")
    p.add_argument("--upcoming-hours", type=float, default=48.0,
                   help="Only scan markets starting within N hours (default 48)")
    p.add_argument("--sport", default=None,
                   help="Filter by sport substring (basketball / baseball / hockey / ...)")
    p.add_argument("--max-signals", type=int, default=50,
                   help="Cap returned signals (default 50)")
    p.add_argument("--save", action="store_true",
                   help="Persist signals to drift_signals table")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip DB writes even if --save")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
