"""Historical signal-to-P&L replay.

Takes rows from a signals table (pitcher_signals / tanking_signals / ...),
simulates what a paper-trade position would have returned. Two exit models:

  - `snapshot` (default) — exits at the last price_snapshot before
    `game_start - HOURS_BEFORE_EXIT` hours, inverted for NO-side favorites.
    Answers: "did the market move toward our bet pre-game?"

  - `resolution` (T-53) — exits at the resolved outcome via
    historical_calibration (won → 1.0 on favored side, lost → 0.0).
    Answers: "did the signal pick the winning team?" This is the true
    strategy-validity test. Required to gate `mlb_pitcher_scanner.enabled`.

Entry is the same for both models: signal.current_price at scan time
(scanner already side-corrects for NO-side favorites).

Stop-loss / take-profit: NOT simulated here; the bot also uses 40% SL/TP
which would change some losing trades into smaller losses. Snapshot-mode
results are therefore a *ceiling* on what the strategy could have produced
under ideal exit timing. Resolution-mode results are a *floor* in the
opposite direction — a real bot would exit pre-game at the snapshot price,
not hold to resolution — but the win/loss direction is authoritative.

The side-resolution step (`resolve_team_token_side`) is the same helper the
live scanner uses post-T-41, so the paper result matches what the bot would
have traded today — it does NOT reproduce what a pre-T-41 bot would have done.

Usage:
    # Snapshot exit (existing behavior)
    python -m analytics.paper_trade_signals \\
        --signal-type pitcher --strength HIGH --output /tmp/pitcher_replay.csv

    # Resolution exit — strategy validity check (T-53)
    python -m analytics.paper_trade_signals \\
        --signal-type pitcher --strength HIGH --exit-model resolution
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional

import asyncpg
import yaml
from tabulate import tabulate

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────────────────
# Per-signal-type configuration
#
# Each entry declares where to read the signal rows and which columns map to
# the fields we need (market_id, scanned_at, game_start, favored_team,
# entry price, strength, action). Adding support for tanking/injury later
# means adding a new dict here — the replay loop is signal-type-agnostic.
# ─────────────────────────────────────────────────────────────────────────────
SIGNAL_CONFIG = {
    "pitcher": {
        "table": "pitcher_signals",
        "aliases_loader": "analytics.mlb_pitcher_scanner:load_mlb_aliases",
        "team_col": "favored_team",
        "price_col": "current_price",
        "strength_col": "signal_strength",
        "action_col": "action",
        "default_action": "BUY",
    },
    "tanking": {
        "table": "tanking_signals",
        "aliases_loader": "analytics.tanking_scanner:load_aliases",
        "team_col": "motivated_team",
        "price_col": "current_price",
        "strength_col": "pattern_strength",
        "action_col": "action",
        "default_action": "BUY",
    },
}


@dataclass
class ReplayTrade:
    signal_id: int
    scanned_at: object
    game_start: object
    market_id: str
    slug: Optional[str]
    team: str
    favored_side: str
    strength: str
    action: str
    entry_price: float
    exit_price: float
    exit_ts: object
    hours_held: float
    pnl_pct: float         # exit - entry, signed
    pnl_usd: float         # pnl_pct * shares; shares = position_size / entry
    win: bool


def _load_callable(path: str):
    """Resolve 'module.sub:func' into an imported callable."""
    mod_path, func_name = path.split(":")
    import importlib
    mod = importlib.import_module(mod_path)
    return getattr(mod, func_name)


async def _fetch_signals(
    conn: asyncpg.Connection,
    cfg: dict,
    strength: Optional[str],
    action: Optional[str],
) -> list[dict]:
    """Fetch the raw signal rows filtered by strength + action."""
    cols = f"id, scanned_at, market_id, game_start, " \
           f"{cfg['team_col']} AS team, " \
           f"{cfg['price_col']} AS entry_price, " \
           f"{cfg['strength_col']} AS strength, " \
           f"{cfg['action_col']} AS action"

    where = []
    params: list = []
    if strength and strength != "all":
        where.append(f"{cfg['strength_col']} = $%d" % (len(params) + 1))
        params.append(strength)
    if action and action != "all":
        where.append(f"{cfg['action_col']} = $%d" % (len(params) + 1))
        params.append(action)

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    sql = f"SELECT {cols} FROM {cfg['table']}{where_sql} ORDER BY scanned_at"
    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def _get_slug(conn: asyncpg.Connection, market_id: str) -> Optional[str]:
    return await conn.fetchval(
        "SELECT slug FROM markets WHERE id = $1", market_id
    )


async def _find_exit_snapshot(
    conn: asyncpg.Connection,
    market_id: str,
    scanned_at,
    game_start,
    hours_before_exit: float,
) -> Optional[tuple[float, object]]:
    """Return (mid_price, ts) of the snapshot the bot would have exited on.

    Rules:
      - exit_target = game_start - hours_before_exit hours
      - prefer the latest snapshot AT OR BEFORE exit_target
      - snapshot must be strictly AFTER scanned_at (otherwise we'd "exit before
        entering" — happens when game_start is too close to scan time)
      - if no such snapshot exists, return None (caller reports 'no exit data')
    """
    if game_start is None:
        return None
    exit_target = game_start - timedelta(hours=hours_before_exit)
    row = await conn.fetchrow(
        """
        SELECT mid_price, ts
        FROM price_snapshots
        WHERE market_id = $1
          AND ts <= $2
          AND ts > $3
        ORDER BY ts DESC
        LIMIT 1
        """,
        market_id, exit_target, scanned_at,
    )
    if row is None:
        return None
    mid = row["mid_price"]
    if mid is None:
        return None
    return float(mid), row["ts"]


async def _find_exit_at_resolution(
    conn: asyncpg.Connection,
    market_id: str,
    side: str,
    game_start,
) -> Optional[tuple[float, object]]:
    """T-53: return (exit_price_on_favored_side, game_start) from resolved outcome.

    Looks up historical_calibration.outcome (0 or 1) and maps to the favored
    side's contract value: 1.0 if favored won, 0.0 if favored lost. The
    "exit timestamp" is game_start since resolution happens at game end —
    good enough for hours_held purposes.
    """
    row = await conn.fetchrow(
        "SELECT outcome FROM historical_calibration WHERE market_id = $1",
        market_id,
    )
    if row is None or row["outcome"] is None:
        return None
    outcome = int(row["outcome"])
    favored_won = (outcome == 1 and side == "YES") or (outcome == 0 and side == "NO")
    exit_price = 1.0 if favored_won else 0.0
    return exit_price, game_start


async def replay(
    conn: asyncpg.Connection,
    signal_type: str,
    strength: Optional[str],
    action: Optional[str],
    position_size_usd: float,
    hours_before_exit: float,
    exit_model: str = "snapshot",
) -> tuple[list[ReplayTrade], dict]:
    """Return (trades, skipped_counts).

    exit_model: "snapshot" (pre-game mid) | "resolution" (T-53: outcome=1|0).
    """
    cfg = SIGNAL_CONFIG[signal_type]

    # Lazy-imported to avoid loading trading/ at module import time
    from trading.position_manager import resolve_team_token_side
    aliases = _load_callable(cfg["aliases_loader"])()

    rows = await _fetch_signals(conn, cfg, strength, action)
    logger.info("fetched %d %s signals (strength=%s, action=%s, exit=%s)",
                len(rows), signal_type, strength, action, exit_model)

    trades: list[ReplayTrade] = []
    skipped = {
        "no_side": 0,
        "no_exit_snapshot": 0,       # used by snapshot model
        "no_resolution": 0,          # used by resolution model (T-53)
        "game_not_passed": 0,
        "zero_or_negative_entry": 0,
    }

    # "game_not_passed" — if game_start is in the future relative to NOW, we
    # can't meaningfully exit yet; skip with this reason so it's visible.
    # pool is in UTC (asyncpg), so compare with NOW() at the DB.
    now = await conn.fetchval("SELECT NOW()")

    for r in rows:
        entry = float(r["entry_price"] or 0.0)
        if entry <= 0 or entry >= 1.0:
            skipped["zero_or_negative_entry"] += 1
            continue

        if r["game_start"] is None or r["game_start"] > now:
            skipped["game_not_passed"] += 1
            continue

        _, side = await resolve_team_token_side(
            conn, r["market_id"], r["team"], aliases
        )
        if side is None:
            skipped["no_side"] += 1
            continue

        if exit_model == "resolution":
            exit_info = await _find_exit_at_resolution(
                conn, r["market_id"], side, r["game_start"],
            )
            if exit_info is None:
                skipped["no_resolution"] += 1
                continue
            exit_price, exit_ts = exit_info
        else:
            exit_info = await _find_exit_snapshot(
                conn, r["market_id"], r["scanned_at"],
                r["game_start"], hours_before_exit,
            )
            if exit_info is None:
                skipped["no_exit_snapshot"] += 1
                continue
            mid, exit_ts = exit_info
            exit_price = mid if side == "YES" else (1.0 - mid)

        pnl_pct = exit_price - entry
        shares = position_size_usd / entry
        pnl_usd = pnl_pct * shares

        held_seconds = (exit_ts - r["scanned_at"]).total_seconds()
        hours_held = held_seconds / 3600.0

        slug = await _get_slug(conn, r["market_id"])

        trades.append(ReplayTrade(
            signal_id=r["id"],
            scanned_at=r["scanned_at"],
            game_start=r["game_start"],
            market_id=r["market_id"],
            slug=slug,
            team=r["team"],
            favored_side=side,
            strength=r["strength"] or "",
            action=r["action"] or "",
            entry_price=round(entry, 4),
            exit_price=round(exit_price, 4),
            exit_ts=exit_ts,
            hours_held=round(hours_held, 2),
            pnl_pct=round(pnl_pct, 4),
            pnl_usd=round(pnl_usd, 2),
            win=pnl_usd > 0,
        ))

    return trades, skipped


def _print_trades(trades: list[ReplayTrade], position_size_usd: float) -> None:
    if not trades:
        print("\n  no trades to report — all signals were skipped\n")
        return
    rows = [[
        t.scanned_at.strftime("%m-%d %H:%M"),
        (t.slug or t.market_id)[:32],
        f"{t.team[:14]} ({t.favored_side})",
        t.strength,
        f"{t.entry_price:.3f}",
        f"{t.exit_price:.3f}",
        f"{t.hours_held:.1f}h",
        f"{t.pnl_pct:+.3f}",
        f"{t.pnl_usd:+.2f}",
        "✓" if t.win else "✗",
    ] for t in trades]
    print("\n" + tabulate(
        rows,
        headers=["scan", "slug", "team (side)", "str", "entry", "exit", "held", "pnl", "pnl $", "w"],
        tablefmt="simple",
    ))


def _print_summary(trades: list[ReplayTrade], skipped: dict, position_size_usd: float) -> None:
    n = len(trades)
    total_skipped = sum(skipped.values())
    print(f"\n{'='*60}")
    print(f"  REPLAY SUMMARY  (position size per trade: ${position_size_usd:.2f})")
    print("="*60)
    print(f"  signals considered : {n + total_skipped}")
    print(f"  skipped            : {total_skipped}")
    for reason, count in skipped.items():
        if count:
            print(f"    - {reason:22s}: {count}")
    print(f"  traded             : {n}")

    if n == 0:
        print("  (no trades to summarize)")
        print("="*60 + "\n")
        return

    wins = sum(1 for t in trades if t.win)
    win_rate = wins / n
    total_pnl = sum(t.pnl_usd for t in trades)
    avg_pnl = total_pnl / n
    total_invested = position_size_usd * n
    roi = total_pnl / total_invested if total_invested > 0 else 0
    avg_hold = sum(t.hours_held for t in trades) / n

    # Wilson score 95% CI for win rate — descriptive only. 50% is NOT the
    # profitability threshold because break-even win rate equals the average
    # entry price per trade; at entry=0.30 a 40% win rate is profitable, at
    # entry=0.60 even 55% loses money. We report this purely as context.
    z = 1.96
    denom = 1 + z * z / n
    center = (win_rate + z * z / (2 * n)) / denom
    margin = z * ((win_rate * (1 - win_rate) + z * z / (4 * n)) / n) ** 0.5 / denom
    wr_ci_lo, wr_ci_hi = max(0.0, center - margin), min(1.0, center + margin)

    # T-56: bootstrap 95% CI on mean pnl_pct (per-share P&L). This IS the
    # authoritative EV test — mean P&L distinguishable from zero is the
    # correct gate for re-enabling a strategy. Seeded RNG for reproducibility.
    pnl_samples = [t.pnl_pct for t in trades]
    mean_pnl_pct = sum(pnl_samples) / n
    rng = random.Random(42)
    bootstrap_means = sorted(
        sum(rng.choice(pnl_samples) for _ in range(n)) / n
        for _ in range(1000)
    )
    pnl_ci_lo = bootstrap_means[24]   # 2.5th percentile of 1000
    pnl_ci_hi = bootstrap_means[974]  # 97.5th percentile of 1000

    print(f"  win rate           : {win_rate:.1%} ({wins}/{n})")
    print(f"  win rate 95% CI    : [{wr_ci_lo:.1%}, {wr_ci_hi:.1%}]  (descriptive)")
    print(f"  avg pnl / share    : {mean_pnl_pct:+.4f}")
    print(f"  pnl/share 95% CI   : [{pnl_ci_lo:+.4f}, {pnl_ci_hi:+.4f}]  (bootstrap)")
    print(f"  avg hold           : {avg_hold:.1f}h")
    print(f"  avg pnl / trade    : ${avg_pnl:+.2f}")
    print(f"  total pnl          : ${total_pnl:+.2f}")
    print(f"  total invested     : ${total_invested:.2f}")
    print(f"  ROI                : {roi:+.1%}")

    if pnl_ci_lo > 0:
        print(f"  verdict            : ✅ significant positive edge (pnl CI lower > 0)")
    elif pnl_ci_hi < 0:
        print(f"  verdict            : ❌ significant negative edge (pnl CI upper < 0)")
    else:
        print(f"  verdict            : ⚠️  inconclusive — pnl CI straddles 0 (need more data)")

    # Breakdown by strength
    by_strength: dict[str, list[ReplayTrade]] = {}
    for t in trades:
        by_strength.setdefault(t.strength, []).append(t)
    if len(by_strength) > 1:
        print("\n  breakdown by strength:")
        for s, tt in sorted(by_strength.items()):
            w = sum(1 for t in tt if t.win)
            pnl = sum(t.pnl_usd for t in tt)
            print(f"    {s:8s}: n={len(tt):3d}  win_rate={w/len(tt):.0%}  total_pnl=${pnl:+.2f}")

    print("="*60 + "\n")


def _write_csv(trades: list[ReplayTrade], path: str) -> None:
    import csv
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scanned_at", "market_id", "slug", "team", "side",
                    "strength", "action", "entry", "exit", "exit_ts",
                    "hours_held", "pnl_pct", "pnl_usd", "win"])
        for t in trades:
            w.writerow([
                t.scanned_at, t.market_id, t.slug or "", t.team, t.favored_side,
                t.strength, t.action, t.entry_price, t.exit_price, t.exit_ts,
                t.hours_held, t.pnl_pct, t.pnl_usd, int(t.win),
            ])
    logger.info("wrote %d rows to %s", len(trades), path)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signal-type", choices=list(SIGNAL_CONFIG.keys()),
                        default="pitcher")
    parser.add_argument("--strength", default="HIGH",
                        help="HIGH | MODERATE | WATCH | all (default: HIGH)")
    parser.add_argument("--action", default="BUY",
                        help="BUY | WATCH | all (default: BUY)")
    parser.add_argument("--position-size", type=float, default=10.0,
                        help="hypothetical USD per trade (default: 10)")
    parser.add_argument("--hours-before-exit", type=float, default=0.5,
                        help="exit this many hours before game_start (default: 0.5)")
    parser.add_argument("--exit-model", choices=["snapshot", "resolution"],
                        default="snapshot",
                        help="snapshot: pre-game close mid (default). "
                             "resolution: held-to-resolution outcome (T-53).")
    parser.add_argument("--output", type=str,
                        help="CSV output path (optional)")
    parser.add_argument("--config", type=str,
                        default=str(_PROJECT_ROOT / "config" / "settings.yaml"))
    args = parser.parse_args()

    cfg = yaml.safe_load(open(args.config))["database"]
    conn = await asyncpg.connect(
        host=cfg["host"], port=cfg["port"], database=cfg["name"],
        user=cfg["user"],
        password=os.environ.get("DB_PASSWORD") or str(cfg["password"]),
    )
    try:
        trades, skipped = await replay(
            conn,
            signal_type=args.signal_type,
            strength=args.strength,
            action=args.action,
            position_size_usd=args.position_size,
            hours_before_exit=args.hours_before_exit,
            exit_model=args.exit_model,
        )
    finally:
        await conn.close()

    _print_trades(trades, args.position_size)
    _print_summary(trades, skipped, args.position_size)

    if args.output:
        _write_csv(trades, args.output)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
