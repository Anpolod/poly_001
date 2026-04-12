"""
analytics/spike_vs_drift_report.py

Batch spike-vs-drift analysis across all markets with >= 20 snapshots.

Per market, applies the same classification logic as timing_analyzer._classify()
and produces:
  - total_move_abs:  abs(last_mid - first_mid) in price units
  - spike_move:      sum of |interval deltas| attributed to spike events
  - drift_move:      sum of |interval deltas| attributed to drift events
  - spike_pct:       spike_move / (spike_move + drift_move)
  - drift_pct:       drift_move / (spike_move + drift_move)
  - n_spikes:        count of snapshot intervals classified as spike
  - largest_spike:   max single interval delta classified as spike (price units)
  - mean_reversion:  True if price returned ≥50% of largest spike within 2h
  - verdict:         FLAT / SPIKE_DRIVEN / DRIFT_DRIVEN / MIXED

Aggregates by sport and across all markets.

Kill signal: if spike_driven_pct > 70% across NBA + football combined →
  prints a WARNING about market structure.

Output:
  - Console tables
  - spike_drift_report.csv (one row per market)

Usage:
    python -m analytics.spike_vs_drift_report
    python analytics/spike_vs_drift_report.py
"""

import asyncio
import csv
import logging
import sys
from collections import defaultdict
from pathlib import Path

import asyncpg
import yaml

try:
    from analytics.timing_analyzer import DRIFT_MIN_MINUTES, SPIKE_THRESHOLD_MULT, _classify
except ModuleNotFoundError:
    # Direct execution: analytics/spike_vs_drift_report.py
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from analytics.timing_analyzer import DRIFT_MIN_MINUTES, SPIKE_THRESHOLD_MULT, _classify

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MIN_SNAPSHOTS = 20

# Verdict thresholds (same as timing_analyzer._render_summary)
FLAT_THRESHOLD_CENTS  = 0.5   # total_abs < 0.5¢
SPIKE_DOMINANT_FRAC   = 0.70  # spike_move > 70% of total → SPIKE_DRIVEN
DRIFT_DOMINANT_FRAC   = 0.50  # drift_move > 50% of total → DRIFT_DRIVEN

# Kill signal threshold
KILL_SPIKE_PCT = 70.0         # % SPIKE_DRIVEN in NBA + football → WARNING

# Mean-reversion window
MEAN_REVERSION_HOURS = 2.0
MEAN_REVERSION_FRAC  = 0.5    # must retrace >= 50% of spike

SQL_MARKETS_WITH_SNAPSHOTS = """
SELECT
    m.id,
    m.slug,
    m.sport,
    m.league,
    COUNT(ps.ts) AS snap_count
FROM markets m
JOIN price_snapshots ps ON ps.market_id = m.id
WHERE ps.mid_price IS NOT NULL
  AND m.status != 'settled'
  AND m.event_start > NOW() - INTERVAL '3 hours'
GROUP BY m.id, m.slug, m.sport, m.league
HAVING COUNT(ps.ts) >= $1
ORDER BY m.sport, snap_count DESC
"""

SQL_SNAPSHOTS_FOR_MARKET = """
SELECT ts, mid_price, spread
FROM price_snapshots
WHERE market_id = $1
  AND mid_price IS NOT NULL
ORDER BY ts
"""


# ---------------------------------------------------------------------------
# Per-market analysis
# ---------------------------------------------------------------------------

def _analyze_market(rows_raw: list[dict]) -> dict:
    """Run classify + extract aggregates for one market. Returns a stats dict."""
    rows = [dict(r) for r in rows_raw]
    for r in rows:
        r["mid_price"] = float(r["mid_price"])
        r["spread"]    = float(r["spread"]) if r["spread"] is not None else None

    rows, avg_speed, spike_thr = _classify(rows)

    if len(rows) < 2:
        return _flat_result()

    first_mid = rows[0]["mid_price"]
    last_mid  = rows[-1]["mid_price"]
    total_move_abs = abs(last_mid - first_mid)
    total_move_cents = total_move_abs * 100.0

    spike_move = 0.0
    drift_move = 0.0
    n_spikes   = 0
    largest_spike_delta = 0.0
    largest_spike_idx   = None

    for i in range(1, len(rows)):
        delta = abs((rows[i]["mid_price"] - rows[i - 1]["mid_price"]) * 100.0)
        etype = rows[i]["event_type"]
        if etype == "spike":
            spike_move += delta
            n_spikes   += 1
            if delta > largest_spike_delta:
                largest_spike_delta = delta
                largest_spike_idx   = i
        elif etype == "drift":
            drift_move += delta

    total_classified = spike_move + drift_move

    spike_pct = (spike_move / total_classified * 100.0) if total_classified > 0 else 0.0
    drift_pct = (drift_move / total_classified * 100.0) if total_classified > 0 else 0.0

    # Mean reversion: did price return ≥50% of largest spike within 2h?
    mean_reversion = False
    if largest_spike_idx is not None:
        spike_ts    = rows[largest_spike_idx]["ts"]
        price_at_spike = rows[largest_spike_idx]["mid_price"]
        reversion_window_sec = MEAN_REVERSION_HOURS * 3600.0

        for j in range(largest_spike_idx + 1, len(rows)):
            dt = (rows[j]["ts"] - spike_ts).total_seconds()
            if dt > reversion_window_sec:
                break
            retrace = abs(rows[j]["mid_price"] - price_at_spike) * 100.0
            if retrace >= largest_spike_delta * MEAN_REVERSION_FRAC:
                mean_reversion = True
                break

    # Verdict
    if total_move_cents < FLAT_THRESHOLD_CENTS:
        verdict = "FLAT"
    elif total_classified > 0 and spike_move > SPIKE_DOMINANT_FRAC * total_classified:
        verdict = "SPIKE_DRIVEN"
    elif total_classified > 0 and drift_move > DRIFT_DOMINANT_FRAC * total_classified:
        verdict = "DRIFT_DRIVEN"
    else:
        verdict = "MIXED"

    return {
        "total_move_abs":  round(total_move_abs, 4),
        "spike_move":      round(spike_move, 4),
        "drift_move":      round(drift_move, 4),
        "spike_pct":       round(spike_pct, 1),
        "drift_pct":       round(drift_pct, 1),
        "n_spikes":        n_spikes,
        "largest_spike":   round(largest_spike_delta, 4),
        "mean_reversion":  mean_reversion,
        "verdict":         verdict,
    }


def _flat_result() -> dict:
    return {
        "total_move_abs": 0.0, "spike_move": 0.0, "drift_move": 0.0,
        "spike_pct": 0.0, "drift_pct": 0.0, "n_spikes": 0,
        "largest_spike": 0.0, "mean_reversion": False, "verdict": "FLAT",
    }


# ---------------------------------------------------------------------------
# Sport-level aggregation
# ---------------------------------------------------------------------------

def _aggregate_by_sport(all_markets: list[dict]) -> dict[str, dict]:
    by_sport: dict[str, list[dict]] = defaultdict(list)
    for m in all_markets:
        by_sport[m["sport"]].append(m)

    agg = {}
    for sport, markets in sorted(by_sport.items()):
        total = len(markets)
        verdicts = [m["verdict"] for m in markets]
        spike_driven_n  = verdicts.count("SPIKE_DRIVEN")
        drift_driven_n  = verdicts.count("DRIFT_DRIVEN")
        mixed_n         = verdicts.count("MIXED")
        flat_n          = verdicts.count("FLAT")

        avg_spike_pct   = sum(m["spike_pct"] for m in markets) / total
        avg_n_spikes    = sum(m["n_spikes"] for m in markets) / total

        # Mean-reversion rate: among markets with at least one spike, % that reverted
        spikey = [m for m in markets if m["n_spikes"] > 0]
        mr_rate = (sum(1 for m in spikey if m["mean_reversion"]) / len(spikey) * 100.0
                   if spikey else 0.0)

        agg[sport] = {
            "total":          total,
            "spike_driven_n": spike_driven_n,
            "drift_driven_n": drift_driven_n,
            "mixed_n":        mixed_n,
            "flat_n":         flat_n,
            "spike_driven_pct": round(spike_driven_n / total * 100, 1),
            "drift_driven_pct": round(drift_driven_n / total * 100, 1),
            "avg_spike_pct":    round(avg_spike_pct, 1),
            "avg_n_spikes":     round(avg_n_spikes, 1),
            "mr_rate":          round(mr_rate, 1),
        }
    return agg


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_table(rows: list[list], headers: list[str]) -> str:
    if not rows:
        return "  (no data)"
    if HAS_TABULATE:
        return tabulate(rows, headers=headers, tablefmt="simple")
    col_w = [max(len(str(h)), max(len(str(r[i])) for r in rows))
             for i, h in enumerate(headers)]
    sep   = "  ".join("-" * w for w in col_w)
    hline = "  ".join(str(h).ljust(w) for h, w in zip(headers, col_w))
    lines = [hline, sep]
    for row in rows:
        lines.append("  ".join(str(v).ljust(w) for v, w in zip(row, col_w)))
    return "\n".join(lines)


def _print_sport_table(agg: dict[str, dict]) -> None:
    headers = ["sport", "markets", "SPIKE%", "DRIFT%", "MIXED%", "FLAT%",
               "avg_spike_pct", "avg_spikes", "mr_rate%"]
    rows = []
    for sport, d in agg.items():
        n = d["total"]
        rows.append([
            sport,
            n,
            f"{d['spike_driven_pct']:.1f}%",
            f"{d['drift_driven_pct']:.1f}%",
            f"{d['mixed_n']/n*100:.1f}%",
            f"{d['flat_n']/n*100:.1f}%",
            f"{d['avg_spike_pct']:.1f}%",
            f"{d['avg_n_spikes']:.1f}",
            f"{d['mr_rate']:.1f}%",
        ])
    print(_fmt_table(rows, headers))


def _print_market_table(markets: list[dict], sport: str, top_n: int = 10) -> None:
    sport_markets = sorted(
        [m for m in markets if m["sport"] == sport],
        key=lambda x: x["spike_pct"], reverse=True
    )[:top_n]
    if not sport_markets:
        return
    headers = ["slug", "snaps", "verdict", "spike%", "drift%",
               "n_spikes", "largest¢", "mean_rev"]
    rows = []
    for m in sport_markets:
        rows.append([
            m["slug"][:45],
            m["snap_count"],
            m["verdict"],
            f"{m['spike_pct']:.1f}%",
            f"{m['drift_pct']:.1f}%",
            m["n_spikes"],
            f"{m['largest_spike']:.4f}",
            "YES" if m["mean_reversion"] else "no",
        ])
    print(_fmt_table(rows, headers))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(config: dict, min_snapshots: int = MIN_SNAPSHOTS, output: str = "spike_drift_report.csv"):
    db = config["database"]
    conn = await asyncpg.connect(
        host=db["host"], port=db["port"], database=db["name"],
        user=db["user"], password=db["password"],
    )

    logger.info(f"Fetching markets with >= {min_snapshots} snapshots…")
    market_rows = await conn.fetch(SQL_MARKETS_WITH_SNAPSHOTS, min_snapshots)

    if not market_rows:
        print(f"No markets with >= {min_snapshots} snapshots found.")
        await conn.close()
        return

    logger.info(f"Analyzing {len(market_rows)} markets…")
    all_markets = []
    for mrow in market_rows:
        snap_rows = await conn.fetch(SQL_SNAPSHOTS_FOR_MARKET, mrow["id"])
        stats = _analyze_market(list(snap_rows))
        all_markets.append({
            "market_id":  mrow["id"],
            "slug":       mrow["slug"],
            "sport":      mrow["sport"],
            "league":     mrow["league"],
            "snap_count": mrow["snap_count"],
            **stats,
        })

    await conn.close()

    agg = _aggregate_by_sport(all_markets)
    total_markets = len(all_markets)
    all_verdicts  = [m["verdict"] for m in all_markets]
    global_spike_pct = all_verdicts.count("SPIKE_DRIVEN") / total_markets * 100

    # --- Console output ---
    print(f"\n{'='*90}")
    print(f"  SPIKE vs DRIFT REPORT — {total_markets} markets, MIN {min_snapshots} snapshots")
    print(f"  Spike threshold: {SPIKE_THRESHOLD_MULT}× avg |speed|   |   "
          f"Drift min duration: {DRIFT_MIN_MINUTES:.0f}min   |   "
          f"Mean-reversion window: {MEAN_REVERSION_HOURS:.0f}h")
    print(f"{'='*90}")

    print("\n── BY SPORT ──────────────────────────────────────────────────────────────────────")
    _print_sport_table(agg)

    for sport in sorted(agg.keys()):
        n = agg[sport]["total"]
        print(f"\n── {sport.upper()} — Top {min(10, n)} by spike% ({'─'*50})")
        _print_market_table(all_markets, sport)

    # --- Key finding ---
    print(f"\n{'='*90}")
    print("  KEY FINDING")
    print(f"{'='*90}")
    print(f"  {global_spike_pct:.1f}% of markets are spike-driven across all sports")
    print(f"  ({all_verdicts.count('SPIKE_DRIVEN')} SPIKE_DRIVEN  |  "
          f"{all_verdicts.count('DRIFT_DRIVEN')} DRIFT_DRIVEN  |  "
          f"{all_verdicts.count('MIXED')} MIXED  |  "
          f"{all_verdicts.count('FLAT')} FLAT)")

    # --- Kill signal ---
    nba_football = [m for m in all_markets if m["sport"] in ("basketball", "football")]
    if nba_football:
        nba_fb_spike_pct = (
            sum(1 for m in nba_football if m["verdict"] == "SPIKE_DRIVEN")
            / len(nba_football) * 100
        )
        if nba_fb_spike_pct > KILL_SPIKE_PCT:
            print(f"\n  {'!'*86}")
            print("  WARNING: market structure is spike-dominated.")
            print(f"  {nba_fb_spike_pct:.1f}% of NBA + football markets are SPIKE_DRIVEN "
                  f"(threshold: {KILL_SPIKE_PCT:.0f}%).")
            print("  Taker directional strategy requires news advantage, not pattern.")
            print(f"  {'!'*86}")
        else:
            print(f"\n  NBA + football spike-driven: {nba_fb_spike_pct:.1f}% "
                  f"(kill threshold: {KILL_SPIKE_PCT:.0f}% — OK)")

    # --- CSV ---
    out_path = Path(output)
    csv_cols = ["market_id", "slug", "sport", "league", "snap_count",
                "total_move_abs", "spike_move", "drift_move",
                "spike_pct", "drift_pct", "n_spikes", "largest_spike",
                "mean_reversion", "verdict"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(all_markets, key=lambda x: x["spike_pct"], reverse=True))

    print(f"\n  CSV written: {out_path} ({total_markets} rows)")
    print(f"{'='*90}")


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Spike vs Drift report — classify markets by price movement type"
    )
    parser.add_argument("--min-snapshots", type=int, default=MIN_SNAPSHOTS, metavar="N",
                        help=f"Minimum snapshots per market (default: {MIN_SNAPSHOTS})")
    parser.add_argument("--output", default="spike_drift_report.csv", metavar="FILE",
                        help="CSV output path (default: spike_drift_report.csv)")
    args = parser.parse_args()

    config_path = Path("config/settings.yaml")
    if not config_path.exists():
        print("ERROR: config/settings.yaml not found")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)

    asyncio.run(run(config, min_snapshots=args.min_snapshots, output=args.output))


if __name__ == "__main__":
    main()
