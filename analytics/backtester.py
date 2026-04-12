"""
Phase 2 — Backtester

Replays price_snapshots and spike_events to evaluate two signal strategies:

  1. DriftSignal   — enter when price moves > threshold¢ over a lookback window
                     without a spike (i.e. smooth drift, not a news jump).
                     Direction: follow the drift (momentum).

  2. ReversionSignal — enter after a spike peak in the opposite direction,
                       expecting mean-reversion back toward the pre-spike level.

Cost model: round-trip taker cost from cost_analysis (spread + fee×2 + slippage).
Positions are sized at 1 unit (normalised). PnL is in price cents (¢).

Usage:
    python -m analytics.backtester
    python -m analytics.backtester --market 1791537
    python -m analytics.backtester --signal drift --lookback 30 --threshold 0.03
    python -m analytics.backtester --signal reversion --hold 60
    python -m analytics.backtester --min-snapshots 200 --output bt_results.csv
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncpg
import pandas as pd
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    ts: datetime
    market_id: str
    mid_price: float
    spread: float
    bid_depth: float
    ask_depth: float


@dataclass
class SpikeEvent:
    market_id: str
    start_ts: datetime
    end_ts: Optional[datetime]
    peak_price: float
    start_price: float
    direction: str   # 'up' | 'down'
    magnitude: float


@dataclass
class Trade:
    """A simulated round-trip trade."""
    market_id: str
    signal: str          # 'drift' | 'reversion'
    direction: str       # 'buy' | 'sell'
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    taker_rt_cost_pct: float   # e.g. 3.98 means 3.98%
    pnl_raw: float       # price move in the position direction (¢)
    pnl_net: float       # pnl_raw minus taker round-trip cost (¢)
    hold_minutes: float
    exit_reason: str     # 'take_profit' | 'stop_loss' | 'timeout' | 'eod'


@dataclass
class BacktestResult:
    market_id: str
    slug: str
    sport: str
    signal: str
    n_trades: int
    win_rate: float
    avg_pnl_net: float       # ¢ per trade
    total_pnl_net: float     # ¢ total
    avg_hold_min: float
    max_drawdown: float
    trades: list[Trade] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _load_snapshots(conn: asyncpg.Connection, market_id: str) -> list[Snapshot]:
    rows = await conn.fetch(
        """
        SELECT ts, market_id,
               mid_price::float, spread::float,
               COALESCE(bid_depth, 0)::float AS bid_depth,
               COALESCE(ask_depth, 0)::float AS ask_depth
        FROM price_snapshots
        WHERE market_id = $1
        ORDER BY ts
        """,
        market_id,
    )
    return [Snapshot(**dict(r)) for r in rows]


async def _load_spikes(conn: asyncpg.Connection, market_id: str) -> list[SpikeEvent]:
    rows = await conn.fetch(
        """
        SELECT market_id, start_ts, end_ts,
               peak_price::float, start_price::float,
               direction, magnitude::float
        FROM spike_events
        WHERE market_id = $1
        ORDER BY start_ts
        """,
        market_id,
    )
    return [SpikeEvent(**dict(r)) for r in rows]


async def _load_markets(
    conn: asyncpg.Connection, min_snapshots: int, market_id: Optional[str]
) -> list[dict]:
    if market_id:
        rows = await conn.fetch(
            """
            SELECT m.id, m.slug, m.sport,
                   COALESCE(ca.taker_rt_cost::float, 4.0) as taker_rt_cost
            FROM markets m
            LEFT JOIN cost_analysis ca ON ca.market_id = m.id
            WHERE m.id = $1
            LIMIT 1
            """,
            market_id,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT m.id, m.slug, m.sport,
                   COALESCE(ca.taker_rt_cost::float, 4.0) as taker_rt_cost
            FROM markets m
            LEFT JOIN (
                SELECT DISTINCT ON (market_id) market_id, taker_rt_cost
                FROM cost_analysis ORDER BY market_id, scanned_at DESC
            ) ca ON ca.market_id = m.id
            WHERE (
                SELECT COUNT(*) FROM price_snapshots ps WHERE ps.market_id = m.id
            ) >= $1
            ORDER BY m.id
            """,
            min_snapshots,
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def _spikes_in_window(spikes: list[SpikeEvent], ts_start: datetime, ts_end: datetime) -> bool:
    """Return True if any spike overlaps the given window."""
    for s in spikes:
        if s.end_ts is None:
            continue
        if s.start_ts < ts_end and s.end_ts > ts_start:
            return True
    return False


def run_drift_signal(
    snapshots: list[Snapshot],
    spikes: list[SpikeEvent],
    taker_rt_cost_pct: float,
    lookback_min: int = 30,
    threshold: float = 0.03,
    hold_min: int = 60,
    stop_loss: float = 0.03,
) -> list[Trade]:
    """
    Drift signal: enter when price moves > threshold¢ over lookback_min minutes
    with no spike in that window (smooth drift, not a news jump).
    Hold for hold_min minutes or exit on stop_loss.
    Direction: follow the drift (momentum).
    """
    trades: list[Trade] = []
    n = len(snapshots)
    in_position = False
    entry_idx = 0
    entry_snap: Optional[Snapshot] = None
    direction = ""
    lookback_sec = lookback_min * 60
    hold_sec = hold_min * 60

    for i, snap in enumerate(snapshots):
        if snap.mid_price is None or snap.mid_price <= 0:
            continue

        if in_position and entry_snap is not None:
            elapsed = (snap.ts - entry_snap.ts).total_seconds()
            price_move = snap.mid_price - entry_snap.entry_price if direction == "buy" \
                else entry_snap.entry_price - snap.mid_price  # type: ignore[attr-defined]
            pnl_raw = price_move

            exit_reason = None
            if pnl_raw <= -stop_loss:
                exit_reason = "stop_loss"
            elif elapsed >= hold_sec:
                exit_reason = "timeout"
            elif i == n - 1:
                exit_reason = "eod"

            if exit_reason:
                cost_frac = taker_rt_cost_pct / 100.0 * entry_snap.entry_price  # type: ignore[attr-defined]
                pnl_net = pnl_raw - cost_frac
                hold_minutes = elapsed / 60.0
                trades.append(Trade(
                    market_id=snap.market_id,
                    signal="drift",
                    direction=direction,
                    entry_ts=entry_snap.ts,  # type: ignore[attr-defined]
                    exit_ts=snap.ts,
                    entry_price=entry_snap.entry_price,  # type: ignore[attr-defined]
                    exit_price=snap.mid_price,
                    taker_rt_cost_pct=taker_rt_cost_pct,
                    pnl_raw=round(pnl_raw, 4),
                    pnl_net=round(pnl_net, 4),
                    hold_minutes=round(hold_minutes, 1),
                    exit_reason=exit_reason,
                ))
                in_position = False
                entry_snap = None
            continue

        # Find snapshot ~lookback_min ago
        window_start_ts = snap.ts.timestamp() - lookback_sec
        candidates = [s for s in snapshots[:i] if s.ts.timestamp() >= window_start_ts]
        if len(candidates) < 3:
            continue

        anchor = candidates[0]
        move = snap.mid_price - anchor.mid_price

        if abs(move) < threshold:
            continue

        # No spike in the drift window
        if _spikes_in_window(spikes, anchor.ts, snap.ts):
            continue

        direction = "buy" if move > 0 else "sell"
        in_position = True
        entry_snap = snap  # type: ignore[assignment]
        entry_snap.entry_price = snap.mid_price  # type: ignore[attr-defined]

    return trades


def run_reversion_signal(
    snapshots: list[Snapshot],
    spikes: list[SpikeEvent],
    taker_rt_cost_pct: float,
    hold_min: int = 60,
    min_magnitude: float = 0.03,
    stop_loss: float = 0.04,
) -> list[Trade]:
    """
    Reversion signal: after a spike peak, enter in the opposite direction
    expecting mean-reversion back toward the pre-spike price.
    Enter at first snapshot after spike end_ts.
    """
    trades: list[Trade] = []
    snap_by_ts = {s.ts: s for s in snapshots}
    snap_list = sorted(snapshots, key=lambda x: x.ts)

    for spike in spikes:
        if spike.end_ts is None or spike.magnitude < min_magnitude:
            continue

        # Direction of trade is opposite to spike direction
        trade_direction = "sell" if spike.direction == "up" else "buy"

        # Find first snapshot after spike end
        entry_snap = next(
            (s for s in snap_list if s.ts > spike.end_ts and s.mid_price is not None),
            None,
        )
        if entry_snap is None:
            continue

        entry_price = entry_snap.mid_price
        hold_sec = hold_min * 60
        exit_snap = None
        exit_reason = "timeout"

        for s in snap_list:
            if s.ts <= entry_snap.ts:
                continue
            elapsed = (s.ts - entry_snap.ts).total_seconds()
            price_move = (
                entry_price - s.mid_price if trade_direction == "sell"
                else s.mid_price - entry_price
            )
            if price_move <= -stop_loss:
                exit_snap = s
                exit_reason = "stop_loss"
                break
            if elapsed >= hold_sec:
                exit_snap = s
                exit_reason = "timeout"
                break

        if exit_snap is None:
            if snap_list:
                exit_snap = snap_list[-1]
                exit_reason = "eod"
            else:
                continue

        exit_price = exit_snap.mid_price
        pnl_raw = (
            entry_price - exit_price if trade_direction == "sell"
            else exit_price - entry_price
        )
        cost_frac = taker_rt_cost_pct / 100.0 * entry_price
        pnl_net = pnl_raw - cost_frac
        hold_minutes = (exit_snap.ts - entry_snap.ts).total_seconds() / 60.0

        trades.append(Trade(
            market_id=entry_snap.market_id,
            signal="reversion",
            direction=trade_direction,
            entry_ts=entry_snap.ts,
            exit_ts=exit_snap.ts,
            entry_price=entry_price,
            exit_price=exit_price,
            taker_rt_cost_pct=taker_rt_cost_pct,
            pnl_raw=round(pnl_raw, 4),
            pnl_net=round(pnl_net, 4),
            hold_minutes=round(hold_minutes, 1),
            exit_reason=exit_reason,
        ))

    return trades


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_result(
    market_id: str, slug: str, sport: str, signal: str, trades: list[Trade]
) -> BacktestResult:
    if not trades:
        return BacktestResult(
            market_id=market_id, slug=slug, sport=sport, signal=signal,
            n_trades=0, win_rate=0.0, avg_pnl_net=0.0,
            total_pnl_net=0.0, avg_hold_min=0.0, max_drawdown=0.0,
        )

    pnls = [t.pnl_net for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    cumulative = []
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        running += p
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
        cumulative.append(running)

    return BacktestResult(
        market_id=market_id,
        slug=slug,
        sport=sport,
        signal=signal,
        n_trades=len(trades),
        win_rate=round(wins / len(trades) * 100, 1),
        avg_pnl_net=round(sum(pnls) / len(pnls), 4),
        total_pnl_net=round(sum(pnls), 4),
        avg_hold_min=round(sum(t.hold_minutes for t in trades) / len(trades), 1),
        max_drawdown=round(max_dd, 4),
        trades=trades,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run(
    config: dict,
    signal: str = "both",
    market_id: Optional[str] = None,
    min_snapshots: int = 100,
    lookback_min: int = 30,
    drift_threshold: float = 0.03,
    hold_min: int = 60,
    stop_loss: float = 0.03,
    min_magnitude: float = 0.03,
    output: Optional[str] = None,
) -> list[BacktestResult]:
    db = config["database"]
    conn = await asyncpg.connect(
        host=db["host"], port=db["port"],
        database=db["name"], user=db["user"], password=db["password"],
    )

    try:
        markets = await _load_markets(conn, min_snapshots, market_id)
        logger.info(f"Backtesting {len(markets)} markets (signal={signal})")

        results: list[BacktestResult] = []

        for m in markets:
            mid = m["id"]
            snapshots = await _load_snapshots(conn, mid)
            spikes = await _load_spikes(conn, mid)
            cost = float(m["taker_rt_cost"])

            if len(snapshots) < min_snapshots:
                continue

            logger.info(
                f"  {m['slug'][:45]:<45} {len(snapshots):>5} snaps "
                f"{len(spikes):>5} spikes  cost={cost:.2f}%"
            )

            if signal in ("drift", "both"):
                trades = run_drift_signal(
                    snapshots, spikes, cost,
                    lookback_min=lookback_min,
                    threshold=drift_threshold,
                    hold_min=hold_min,
                    stop_loss=stop_loss,
                )
                results.append(compute_result(mid, m["slug"], m["sport"], "drift", trades))

            if signal in ("reversion", "both"):
                trades = run_reversion_signal(
                    snapshots, spikes, cost,
                    hold_min=hold_min,
                    min_magnitude=min_magnitude,
                    stop_loss=stop_loss,
                )
                results.append(compute_result(mid, m["slug"], m["sport"], "reversion", trades))

    finally:
        await conn.close()

    # --- Print summary ---
    summary_rows = [
        {
            "slug": r.slug[:40],
            "sport": r.sport,
            "signal": r.signal,
            "trades": r.n_trades,
            "win%": r.win_rate,
            "avg_pnl¢": r.avg_pnl_net,
            "total_pnl¢": r.total_pnl_net,
            "avg_hold_min": r.avg_hold_min,
            "max_dd¢": r.max_drawdown,
        }
        for r in results if r.n_trades > 0
    ]

    if not summary_rows:
        print("\nNo trades generated — try lower --threshold or --min-magnitude\n")
        return results

    df = pd.DataFrame(summary_rows).sort_values("total_pnl¢", ascending=False)

    print(f"\n{'='*100}")
    print(f"  BACKTEST RESULTS — {len(summary_rows)} market/signal combos with trades")
    print(f"  Signal: {signal}  |  Lookback: {lookback_min}min  |  Threshold: {drift_threshold}¢"
          f"  |  Hold: {hold_min}min  |  StopLoss: {stop_loss}¢")
    print(f"{'='*100}")
    print(df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # Aggregate by signal type
    for sig in ("drift", "reversion"):
        sig_rows = [r for r in results if r.signal == sig and r.n_trades > 0]
        if not sig_rows:
            continue
        total_trades = sum(r.n_trades for r in sig_rows)
        total_pnl = sum(r.total_pnl_net for r in sig_rows)
        all_wins = sum(
            sum(1 for t in r.trades if t.pnl_net > 0) for r in sig_rows
        )
        win_rate = all_wins / total_trades * 100 if total_trades else 0
        print(f"\n  [{sig.upper()}] {len(sig_rows)} markets | "
              f"{total_trades} trades | win rate {win_rate:.1f}% | "
              f"total PnL {total_pnl:+.4f}¢ | avg/trade {total_pnl/total_trades:+.4f}¢")

    print(f"\n{'='*100}\n")

    if output:
        # Flatten all trades to CSV
        all_trades = [
            {
                "market_id": t.market_id,
                "slug": next((r.slug for r in results if r.market_id == t.market_id), ""),
                "sport": next((r.sport for r in results if r.market_id == t.market_id), ""),
                "signal": t.signal,
                "direction": t.direction,
                "entry_ts": t.entry_ts,
                "exit_ts": t.exit_ts,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "taker_rt_cost_pct": t.taker_rt_cost_pct,
                "pnl_raw": t.pnl_raw,
                "pnl_net": t.pnl_net,
                "hold_minutes": t.hold_minutes,
                "exit_reason": t.exit_reason,
            }
            for r in results for t in r.trades
        ]
        pd.DataFrame(all_trades).to_csv(output, index=False)
        logger.info(f"Trade log saved to {output}")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest drift and reversion signals on collected price_snapshots"
    )
    p.add_argument("--market", default=None, help="Single market_id to test")
    p.add_argument(
        "--signal", choices=["drift", "reversion", "both"], default="both",
        help="Which signal to run (default: both)",
    )
    p.add_argument("--min-snapshots", type=int, default=100,
                   help="Minimum snapshots required per market (default: 100)")
    p.add_argument("--lookback", type=int, default=30,
                   help="Drift lookback window in minutes (default: 30)")
    p.add_argument("--threshold", type=float, default=0.03,
                   help="Drift threshold in price units ¢ (default: 0.03)")
    p.add_argument("--hold", type=int, default=60,
                   help="Max hold time in minutes (default: 60)")
    p.add_argument("--stop-loss", type=float, default=0.03,
                   help="Stop loss in price units ¢ (default: 0.03)")
    p.add_argument("--min-magnitude", type=float, default=0.03,
                   help="Minimum spike magnitude for reversion signal (default: 0.03)")
    p.add_argument("--output", default=None, help="Save trade log to CSV")
    p.add_argument("--config", default="config/settings.yaml", help="Path to settings YAML")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: {args.config} not found.")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)

    asyncio.run(run(
        config,
        signal=args.signal,
        market_id=args.market,
        min_snapshots=args.min_snapshots,
        lookback_min=args.lookback,
        drift_threshold=args.threshold,
        hold_min=args.hold,
        stop_loss=args.stop_loss,
        min_magnitude=args.min_magnitude,
        output=args.output,
    ))
