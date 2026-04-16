"""
Spike Follow Signal (T-31)

Reads finalized spike events from `spike_events` (populated by the collector's
SpikeTracker via ws_client) and produces follow-the-direction signals when:
  - magnitude >= min_magnitude (default 5¢)
  - n_steps >= min_steps (default 4)
  - hours_to_game > min_hours_to_game (default 1h)

V1 is alert-only — direction is correct ('up' / 'down' from spike_events) but
the YES/NO mapping to the right Polymarket token still needs the same
resolve_team_token_id() helper that's blocked behind T-35.

Usage:
    python -m analytics.spike_signal                          # default thresholds
    python -m analytics.spike_signal --since-min 60           # last 60 min only
    python -m analytics.spike_signal --min-magnitude 0.04
    python -m analytics.spike_signal --save
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
class SpikeSignal:
    spike_event_id: int
    market_id: str
    slug: str
    sport: str
    game_start: Optional[datetime]
    hours_to_game: float
    direction: str            # 'up' / 'down' (matches spike_events.direction)
    magnitude: float
    n_steps: int
    entry_price: float        # end_price of the spike
    action: str               # BUY / WATCH
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
    since_minutes: int = 30,
    min_magnitude: float = 0.05,
    min_steps: int = 4,
    min_hours_to_game: float = 1.0,
    upcoming_hours: float = 48.0,
    max_signals: int = 30,
) -> list[SpikeSignal]:
    """Read spike_events for the last `since_minutes` and produce SpikeSignal rows.

    Joins to `markets` for game_start + sport. Filters by upcoming game window
    so we don't follow spikes on markets that already settled or won't settle
    soon enough to act on.
    """
    rows = await pool.fetch(
        """
        SELECT
            s.id            AS spike_id,
            s.market_id,
            s.start_ts,
            s.end_ts,
            s.end_price::float       AS end_price,
            s.peak_price::float      AS peak_price,
            s.start_price::float     AS start_price,
            s.magnitude::float       AS magnitude,
            s.direction,
            s.n_steps,
            m.slug,
            m.sport,
            m.event_start
        FROM spike_events s
        JOIN markets m ON m.id = s.market_id
        WHERE s.start_ts >= NOW() - ($1 * INTERVAL '1 minute')
          AND s.magnitude >= $2
          AND s.n_steps   >= $3
          AND m.status = 'active'
          AND m.event_start BETWEEN
              NOW() + ($4 * INTERVAL '1 hour') AND
              NOW() + ($5 * INTERVAL '1 hour')
        ORDER BY s.start_ts DESC
        LIMIT $6
        """,
        int(since_minutes),
        float(min_magnitude),
        int(min_steps),
        float(min_hours_to_game),
        float(upcoming_hours),
        int(max_signals),
    )

    if not rows:
        return []

    now = datetime.now(tz=timezone.utc)
    signals: list[SpikeSignal] = []
    for r in rows:
        event_start = r["event_start"]
        if event_start and event_start.tzinfo is None:
            event_start = event_start.replace(tzinfo=timezone.utc)
        hours_to_game = (event_start - now).total_seconds() / 3600 if event_start else 0.0

        # Use end_price as entry — that's where the spike actually settled.
        # Fall back to peak_price if end_price is null (in-progress finalizations).
        entry = float(r["end_price"]) if r["end_price"] is not None else float(r["peak_price"] or 0)
        if entry <= 0:
            continue

        signals.append(SpikeSignal(
            spike_event_id=int(r["spike_id"]),
            market_id=r["market_id"],
            slug=r["slug"] or r["market_id"],
            sport=r["sport"] or "other",
            game_start=event_start,
            hours_to_game=round(hours_to_game, 2),
            direction=r["direction"] or "?",
            magnitude=round(float(r["magnitude"]), 4),
            n_steps=int(r["n_steps"] or 0),
            entry_price=round(entry, 4),
            action="BUY",   # follow direction; YES/NO mapping resolved by T-35 helper
            notes=f"start={float(r['start_price'] or 0):.3f} peak={float(r['peak_price'] or 0):.3f}",
        ))

    return signals


async def persist_signals(pool: asyncpg.Pool, signals: list[SpikeSignal]) -> int:
    """Insert into spike_signals; dedupe on spike_event_id (one signal per event)."""
    inserted = 0
    for s in signals:
        exists = await pool.fetchval(
            "SELECT 1 FROM spike_signals WHERE spike_event_id = $1 LIMIT 1",
            s.spike_event_id,
        )
        if exists:
            continue
        await pool.execute(
            """
            INSERT INTO spike_signals
                (spike_event_id, market_id, sport, game_start,
                 direction, magnitude, n_steps, entry_price, action, notes)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            s.spike_event_id, s.market_id, s.sport, s.game_start,
            s.direction, s.magnitude, s.n_steps, s.entry_price, s.action, s.notes,
        )
        inserted += 1
    return inserted


def print_signals(signals: list[SpikeSignal]) -> None:
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("\n" + "=" * 100)
    print(f"  SPIKE FOLLOW — {now_str}")
    print("=" * 100)
    if not signals:
        print("\n  No spike events matched the filter window.\n")
        print("=" * 100 + "\n")
        return

    print(f"  {'sport':12s}  {'dir':>4s}  {'mag':>6s}  {'steps':>5s}  "
          f"{'entry':>6s}  {'T-h':>6s}  slug")
    print("  " + "-" * 96)
    for s in signals:
        print(f"  {s.sport[:12]:12s}  {s.direction:>4s}  {s.magnitude:6.4f}  "
              f"{s.n_steps:5d}  {s.entry_price:6.3f}  {s.hours_to_game:6.1f}  "
              f"{s.slug[:50]}")
    print("\n" + "=" * 100 + "\n")


async def _main(args: argparse.Namespace) -> None:
    config = _load_config()
    pool = await _create_pool(config)
    try:
        signals = await scan(
            pool,
            since_minutes=args.since_min,
            min_magnitude=args.min_magnitude,
            min_steps=args.min_steps,
            min_hours_to_game=args.min_hours,
            upcoming_hours=args.upcoming_hours,
            max_signals=args.max_signals,
        )
        print_signals(signals)
        if args.save and not args.dry_run and signals:
            n = await persist_signals(pool, signals)
            print(f"  Saved {n} new spike_signals row(s).\n")
    finally:
        await pool.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Spike follow signal generator")
    p.add_argument("--since-min", type=int, default=30,
                   help="Look at spike_events from the last N minutes (default 30)")
    p.add_argument("--min-magnitude", type=float, default=0.05,
                   help="Minimum spike magnitude (default 0.05 = 5¢)")
    p.add_argument("--min-steps", type=int, default=4,
                   help="Minimum consecutive same-direction steps (default 4)")
    p.add_argument("--min-hours", type=float, default=1.0,
                   help="Minimum hours_to_game (default 1.0)")
    p.add_argument("--upcoming-hours", type=float, default=48.0,
                   help="Maximum hours_to_game (default 48)")
    p.add_argument("--max-signals", type=int, default=30,
                   help="Cap returned signals (default 30)")
    p.add_argument("--save", action="store_true",
                   help="Persist signals to spike_signals table")
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
