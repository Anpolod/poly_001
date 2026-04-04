"""
analytics/timing_analyzer.py

Per-market price movement timing analysis.

Fetches all price_snapshots for a market and classifies each snapshot
as 'stable', 'drift', or 'spike' to answer:
  Is this move tradeable (gradual drift) or untrackeable (instant spike)?

Usage:
    python -m analytics.timing_analyzer --market nba-det-phi-2026-04-04
    python -m analytics.timing_analyzer --market nba-det-phi-2026-04-04 --hours 24
    python -m analytics.timing_analyzer --market nba-det-phi-2026-04-04 --limit 100
    python -m analytics.timing_analyzer --market nba-det-phi-2026-04-04 --summary-only
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import asyncpg
import yaml

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- Tunable constants ---
SPIKE_THRESHOLD_MULT = 3.0   # |speed| > N * avg_speed → spike
DRIFT_MIN_MINUTES = 30.0     # sustained directional move must span >= N min


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

SQL_FIND_MARKET = """
SELECT id, slug, question, sport, league, event_start
FROM markets
WHERE slug ILIKE $1
   OR id   ILIKE $1
ORDER BY event_start DESC
LIMIT 10
"""

SQL_SNAPSHOTS_ALL = """
SELECT ts, mid_price, spread
FROM price_snapshots
WHERE market_id = $1
  AND mid_price IS NOT NULL
ORDER BY ts
"""

SQL_SNAPSHOTS_HOURS = """
SELECT ts, mid_price, spread
FROM price_snapshots
WHERE market_id = $1
  AND mid_price IS NOT NULL
  AND ts >= NOW() - ($2 || ' hours')::INTERVAL
ORDER BY ts
"""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify(rows: list[dict]) -> tuple[list[dict], float, float]:
    """
    Annotate each row with:
      minutes_elapsed  — minutes since first snapshot
      cumulative_move  — (mid - first_mid) in cents
      move_speed       — cents/minute since prior snapshot
      event_type       — 'stable' | 'drift' | 'spike'

    Returns (rows, avg_abs_speed, spike_threshold).
    """
    if len(rows) < 2:
        for r in rows:
            r.update(minutes_elapsed=0.0, cumulative_move=0.0,
                     move_speed=0.0, is_spike=False, event_type="stable")
        return rows, 0.0, 0.0

    first_mid = rows[0]["mid_price"]
    first_ts = rows[0]["ts"]

    # Pass 1 — per-snapshot speed & cumulative move
    for i, row in enumerate(rows):
        elapsed = (row["ts"] - first_ts).total_seconds() / 60.0
        row["minutes_elapsed"] = elapsed
        row["cumulative_move"] = (row["mid_price"] - first_mid) * 100.0

        if i == 0:
            row["move_speed"] = 0.0
        else:
            prev = rows[i - 1]
            dt_min = (row["ts"] - prev["ts"]).total_seconds() / 60.0
            if dt_min > 0:
                row["move_speed"] = (row["mid_price"] - prev["mid_price"]) * 100.0 / dt_min
            else:
                row["move_speed"] = 0.0

    # Average absolute speed (exclude the always-zero first row)
    abs_speeds = [abs(r["move_speed"]) for r in rows[1:]]
    avg_speed = sum(abs_speeds) / len(abs_speeds) if abs_speeds else 0.0
    spike_thr = SPIKE_THRESHOLD_MULT * avg_speed

    # Pass 2 — spike flag
    for row in rows:
        row["is_spike"] = avg_speed > 0 and abs(row["move_speed"]) > spike_thr

    # Pass 3 — drift detection
    # A drift is a contiguous run of non-spike snapshots moving in the same
    # direction that spans >= DRIFT_MIN_MINUTES.
    in_drift = [False] * len(rows)
    i = 0
    while i < len(rows):
        row = rows[i]
        if row["is_spike"] or row["move_speed"] == 0.0:
            i += 1
            continue

        direction = 1 if row["move_speed"] > 0 else -1
        j = i
        while j < len(rows):
            r = rows[j]
            if r["is_spike"]:
                break
            spd = r["move_speed"]
            spd_dir = 0 if spd == 0.0 else (1 if spd > 0 else -1)
            if spd_dir != 0 and spd_dir != direction:
                break
            j += 1

        run_dur = (rows[j - 1]["ts"] - rows[i]["ts"]).total_seconds() / 60.0
        if run_dur >= DRIFT_MIN_MINUTES:
            for k in range(i, j):
                in_drift[k] = True

        i = j if j > i else i + 1

    # Pass 4 — final classification
    for i, row in enumerate(rows):
        if row["is_spike"]:
            row["event_type"] = "spike"
        elif in_drift[i]:
            row["event_type"] = "drift"
        else:
            row["event_type"] = "stable"

    return rows, avg_speed, spike_thr


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_TYPE_LABEL = {
    "spike":  "*** SPIKE",
    "drift":  "  ~ drift",
    "stable": "        .",
}


def _render_chart(rows: list[dict], market: dict, limit: int) -> str:
    lines: list[str] = []

    lines.append("")
    lines.append(f"Market : {market['slug']}")
    lines.append(f"         {market['question']}")
    lines.append(f"Sport  : {market['sport']} / {market['league']}")
    w_start = rows[0]["ts"].strftime("%Y-%m-%d %H:%M UTC")
    w_end   = rows[-1]["ts"].strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"Window : {w_start}  →  {w_end}")
    lines.append(f"Rows   : {len(rows)} snapshots total")

    display = rows
    if limit > 0 and len(rows) > limit:
        skipped = len(rows) - limit
        lines.append(f"         (showing last {limit}; {skipped} earlier rows hidden — use --limit 0 for all)")
        display = rows[-limit:]
    lines.append("")

    header = f"{'Time':>6} | {'Mid':>6} | {'Move':>8} | {'Spd¢/m':>8} | {'Sprd':>6} | Type"
    sep    = "-" * len(header)
    lines.append(header)
    lines.append(sep)

    first_ts = rows[0]["ts"]  # always relative to very first snapshot
    for row in display:
        elapsed_m = int(row["minutes_elapsed"])
        hh, mm    = divmod(elapsed_m, 60)
        time_str  = f"{hh:02d}:{mm:02d}"

        mid      = row["mid_price"]
        move     = row["cumulative_move"]
        spd      = row["move_speed"]
        spread_c = (row["spread"] or 0.0) * 100.0
        label    = _TYPE_LABEL.get(row["event_type"], "")

        lines.append(
            f"{time_str:>6} | {mid:.4f} | {move:>+7.2f}¢ | {spd:>+7.4f} | {spread_c:>5.2f}¢ | {label}"
        )

    return "\n".join(lines)


def _render_summary(rows: list[dict], avg_speed: float, spike_thr: float) -> str:
    if len(rows) < 2:
        return "Not enough data (< 2 snapshots)."

    first_mid = rows[0]["mid_price"]
    last_mid  = rows[-1]["mid_price"]
    total_move = (last_mid - first_mid) * 100.0
    total_abs  = abs(total_move)

    # Per-interval deltas attributed to event type of the destination row
    spike_move = 0.0
    drift_move = 0.0
    largest_spike_delta = 0.0
    largest_spike_ts    = None

    for i in range(1, len(rows)):
        delta   = abs((rows[i]["mid_price"] - rows[i - 1]["mid_price"]) * 100.0)
        etype   = rows[i]["event_type"]
        if etype == "spike":
            spike_move += delta
            if delta > largest_spike_delta:
                largest_spike_delta = delta
                largest_spike_ts    = rows[i]["ts"]
        elif etype == "drift":
            drift_move += delta

    drift_rows = [r for r in rows if r["event_type"] == "drift"]
    avg_spread_drift: Optional[float] = (
        sum((r["spread"] or 0.0) * 100.0 for r in drift_rows) / len(drift_rows)
        if drift_rows else None
    )

    lines: list[str] = []
    lines.append("")
    lines.append("=" * 62)
    lines.append("  SUMMARY")
    lines.append("=" * 62)
    lines.append(f"  Total move:            {total_move:>+7.2f}¢")

    if total_abs > 0:
        lines.append(f"  Via spike:             {spike_move:>7.2f}¢  ({spike_move / total_abs * 100:>4.0f}%)  ← news / untradeable")
        lines.append(f"  Via drift:             {drift_move:>7.2f}¢  ({drift_move / total_abs * 100:>4.0f}%)  ← potentially tradeable")
    else:
        lines.append("  Via spike:               0.00¢  (  0%)")
        lines.append("  Via drift:               0.00¢  (  0%)")

    if largest_spike_ts:
        lines.append(f"  Largest single spike:  {largest_spike_delta:>7.2f}¢  at {largest_spike_ts.strftime('%H:%M')}")
    else:
        lines.append("  Largest single spike:     none")

    if avg_spread_drift is not None:
        lines.append(f"  Avg spread (drift):    {avg_spread_drift:>7.2f}¢")
    else:
        lines.append("  Avg spread (drift):       n/a  (no drift periods detected)")

    lines.append(f"\n  Spike threshold:       >{spike_thr:.4f} ¢/min  "
                 f"({SPIKE_THRESHOLD_MULT}× avg |speed| {avg_speed:.4f})")
    lines.append(f"  Drift min duration:    {DRIFT_MIN_MINUTES:.0f} min")

    # Tradeable verdict
    lines.append("")
    lines.append("  ── TRADEABLE ASSESSMENT ──────────────────────────────")
    if total_abs < 0.5:
        verdict = "FLAT — move too small to assess (<0.5¢)"
    elif spike_move > 0.70 * total_abs:
        verdict = "SPIKE-DRIVEN — likely news event; NOT tradeable via drift strategy"
    elif drift_move > 0.50 * total_abs:
        verdict = "DRIFT-DRIVEN — gradual repricing detected; POTENTIALLY TRADEABLE"
    else:
        verdict = "MIXED — blend of drift and spikes; review chart for entry windows"

    lines.append(f"  {verdict}")
    lines.append("=" * 62)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(market_slug: str, hours: Optional[int], limit: int,
              summary_only: bool, config: dict) -> None:
    db = config["database"]
    conn = await asyncpg.connect(
        host=db["host"], port=db["port"], database=db["name"],
        user=db["user"], password=db["password"],
    )

    # --- Resolve market ---
    markets = await conn.fetch(SQL_FIND_MARKET, f"%{market_slug}%")
    if not markets:
        print(f"ERROR: No market found matching '{market_slug}'")
        await conn.close()
        return

    if len(markets) > 1:
        print(f"Multiple markets match '{market_slug}':")
        for m in markets:
            print(f"  {m['slug']:50s}  ({m['id']})")
        print(f"\nUsing first: {markets[0]['slug']}\n")

    market = dict(markets[0])
    market_id = market["id"]

    # --- Fetch snapshots ---
    if hours:
        rows_raw = await conn.fetch(SQL_SNAPSHOTS_HOURS, market_id, str(hours))
    else:
        rows_raw = await conn.fetch(SQL_SNAPSHOTS_ALL, market_id)

    await conn.close()

    if not rows_raw:
        scope = f"last {hours}h" if hours else "all time"
        print(f"No snapshots found for '{market['slug']}' ({scope}).")
        return

    rows = [dict(r) for r in rows_raw]
    for r in rows:
        r["mid_price"] = float(r["mid_price"])
        r["spread"]    = float(r["spread"]) if r["spread"] is not None else None

    rows, avg_speed, spike_thr = _classify(rows)

    if not summary_only:
        print(_render_chart(rows, market, limit))
    print(_render_summary(rows, avg_speed, spike_thr))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Timing analyzer — classify price snapshots as stable / drift / spike"
    )
    parser.add_argument("--market", required=True,
                        help="Market slug or ID (partial ILIKE match)")
    parser.add_argument("--hours", type=int, default=None,
                        help="Restrict to last N hours of data")
    parser.add_argument("--limit", type=int, default=200,
                        help="Max rows shown in ASCII chart (0 = all, default 200)")
    parser.add_argument("--summary-only", action="store_true",
                        help="Skip row-by-row chart, show only summary block")
    args = parser.parse_args()

    config_path = Path("config/settings.yaml")
    if not config_path.exists():
        print("ERROR: config/settings.yaml not found. Run from project root.")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)

    asyncio.run(run(args.market, args.hours, args.limit, args.summary_only, config))


if __name__ == "__main__":
    main()
