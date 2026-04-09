"""
Obsidian Reporter

Generates markdown reports from prop_scan_log and historical_calibration tables,
writing them into the obsidian/ directory so Obsidian picks them up automatically.

Usage:
    python -m analytics.obsidian_reporter                     # all reports for today
    python -m analytics.obsidian_reporter --date 2026-04-05   # specific date
    python -m analytics.obsidian_reporter --report daily      # only daily scan log
    python -m analytics.obsidian_reporter --report pnl        # only P&L tracker
    python -m analytics.obsidian_reporter --report calibration # only calibration overview
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_OBSIDIAN_ROOT = _PROJECT_ROOT / "Polymarket"


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


async def _pool(config: dict) -> asyncpg.Pool:
    db = config["database"]
    return await asyncpg.create_pool(
        host=db["host"],
        port=db["port"],
        database=db["name"],
        user=db["user"],
        password=str(db["password"]),
        min_size=1,
        max_size=3,
    )


# ---------------------------------------------------------------------------
# Report: Daily prop scan log
# ---------------------------------------------------------------------------


async def report_daily(pool: asyncpg.Pool, target_date: date) -> Path:
    """Write obsidian/Prop Scanner/YYYY-MM-DD.md."""
    date_str = target_date.isoformat()
    rows = await pool.fetch(
        """
        SELECT player_name, prop_type, threshold, hours_until,
               yes_price, model_win, ev_per_unit, roi_pct,
               bid_depth, ask_depth, outcome, alerted,
               scanned_at
        FROM prop_scan_log
        WHERE scanned_at >= $1 AND scanned_at < $1 + INTERVAL '1 day'
        ORDER BY roi_pct DESC
        """,
        datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc),
    )

    out_dir = _ensure(_OBSIDIAN_ROOT / "Prop Scanner")
    out_file = out_dir / f"{date_str}.md"

    total = len(rows)
    best_roi = max((float(r["roi_pct"]) for r in rows), default=0.0)
    resolved = [r for r in rows if r["outcome"] is not None]
    wins = sum(1 for r in resolved if r["outcome"] == 1)
    win_rate = wins / len(resolved) * 100 if resolved else None

    lines = [
        "---",
        f"date: {date_str}",
        "type: daily_prop_scan",
        f"opportunities: {total}",
        f"best_roi: {best_roi:.1f}",
        "---",
        "",
        f"# Prop Scanner — {date_str}",
        "",
    ]

    if not rows:
        lines += ["*No opportunities logged today.*", ""]
    else:
        lines += [
            "## Today's Opportunities",
            "",
            "| Player | Type | Thresh | Game h | Price | ModelWin | EV/unit | ROI% | BidSz$ | AskSz$ | Outcome |",
            "|--------|------|--------|--------|-------|----------|---------|------|--------|--------|---------|",
        ]
        for r in rows:
            outcome_str = {1: "✅ YES", 0: "❌ NO"}.get(r["outcome"], "—")
            lines.append(
                f"| {r['player_name']} "
                f"| {r['prop_type']} "
                f"| {r['threshold']} "
                f"| {r['hours_until']:.1f}h "
                f"| {r['yes_price']:.3f} "
                f"| {r['model_win']:.3f} "
                f"| {r['ev_per_unit']:+.4f} "
                f"| {r['roi_pct']:+.1f}% "
                f"| {r['bid_depth']:.0f} "
                f"| {r['ask_depth']:.0f} "
                f"| {outcome_str} |"
            )

        lines.append("")
        if resolved:
            lines += [
                "## Resolution Summary",
                "",
                f"- Resolved: **{len(resolved)}** / {total}",
                f"- Win rate: **{win_rate:.1f}%**",
                "",
            ]

    out_file.write_text("\n".join(lines), encoding="utf-8")
    return out_file


# ---------------------------------------------------------------------------
# Report: Calibration overview
# ---------------------------------------------------------------------------


async def report_calibration(pool: asyncpg.Pool) -> Path:
    """Write obsidian/Calibration/player-props.md."""
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Summary by market_type
    type_rows = await pool.fetch(
        """
        SELECT market_type,
               COUNT(*)            AS n,
               AVG(price_close)    AS avg_price,
               AVG(outcome)        AS actual_win_rate
        FROM historical_calibration
        WHERE outcome IS NOT NULL AND market_type IS NOT NULL
        GROUP BY market_type
        ORDER BY market_type
        """
    )

    # Price-bucket calibration across all prop types
    bucket_rows = await pool.fetch(
        """
        SELECT market_type,
               CASE
                   WHEN price_close < 0.30 THEN '<30%'
                   WHEN price_close < 0.40 THEN '30-40%'
                   WHEN price_close < 0.50 THEN '40-50%'
                   WHEN price_close < 0.60 THEN '50-60%'
                   ELSE '>=60%'
               END                     AS bucket,
               COUNT(*)                AS n,
               AVG(price_close)        AS avg_price,
               AVG(outcome::float)     AS actual_win,
               AVG(outcome::float) - AVG(price_close) AS edge_pp
        FROM historical_calibration
        WHERE outcome IS NOT NULL AND price_close IS NOT NULL AND market_type IS NOT NULL
        GROUP BY market_type, bucket
        ORDER BY market_type, avg_price
        """
    )

    out_dir = _ensure(_OBSIDIAN_ROOT / "Calibration")
    out_file = out_dir / "player-props.md"

    lines = [
        "---",
        "type: calibration_overview",
        f"updated: {now_str}",
        "---",
        "",
        "# NBA Player Props Calibration",
        "",
        f"*Last updated: {now_str}*",
        "",
        "## By Prop Type",
        "",
        "| Type | N markets | Avg close price | Actual win rate | Edge (pp) |",
        "|------|-----------|-----------------|-----------------|-----------|",
    ]

    for r in type_rows:
        edge = float(r["actual_win_rate"]) - float(r["avg_price"])
        lines.append(
            f"| {r['market_type']} "
            f"| {r['n']:,} "
            f"| {float(r['avg_price']):.3f} "
            f"| {float(r['actual_win_rate']):.3f} "
            f"| {edge:+.3f} |"
        )

    lines += ["", "## By Price Bucket", ""]

    for mtype in sorted({r["market_type"] for r in bucket_rows}):
        lines += [f"### {mtype.title()}", "", "| Bucket | N | Avg price | Actual win | Edge (pp) |", "|--------|---|-----------|------------|-----------|"]
        for r in bucket_rows:
            if r["market_type"] != mtype:
                continue
            lines.append(
                f"| {r['bucket']} "
                f"| {r['n']:,} "
                f"| {float(r['avg_price']):.3f} "
                f"| {float(r['actual_win']):.3f} "
                f"| {float(r['edge_pp']):+.3f} |"
            )
        lines.append("")

    lines += [
        "## Notes",
        "",
        "- Edge (pp) = actual win rate − implied price. Positive = YES underpriced.",
        "- Price bucket 30–50% shows strongest systematic edge for NBA props.",
        "- Source: `historical_calibration` table populated by `analytics/historical_fetcher.py`.",
        "",
    ]

    out_file.write_text("\n".join(lines), encoding="utf-8")
    return out_file


# ---------------------------------------------------------------------------
# Report: P&L tracker
# ---------------------------------------------------------------------------


async def report_pnl(pool: asyncpg.Pool) -> Path:
    """Write obsidian/P&L/tracker.md."""
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    overall = await pool.fetchrow(
        """
        SELECT
            COUNT(*)                                                     AS total,
            COUNT(*) FILTER (WHERE outcome IS NOT NULL)                  AS resolved,
            COUNT(*) FILTER (WHERE outcome = 1)                          AS wins,
            AVG(roi_pct)                                                 AS avg_roi,
            SUM(CASE WHEN outcome = 1 THEN (1 - 0.03 - yes_price)
                     WHEN outcome = 0 THEN -yes_price
                     ELSE 0 END)                                         AS total_pnl
        FROM prop_scan_log
        """
    )

    by_type = await pool.fetch(
        """
        SELECT
            prop_type,
            COUNT(*)                                                     AS total,
            COUNT(*) FILTER (WHERE outcome IS NOT NULL)                  AS resolved,
            COUNT(*) FILTER (WHERE outcome = 1)                          AS wins,
            AVG(roi_pct)                                                 AS avg_roi,
            SUM(CASE WHEN outcome = 1 THEN (1 - 0.03 - yes_price)
                     WHEN outcome = 0 THEN -yes_price
                     ELSE 0 END)                                         AS total_pnl
        FROM prop_scan_log
        GROUP BY prop_type
        ORDER BY prop_type
        """
    )

    recent = await pool.fetch(
        """
        SELECT (scanned_at AT TIME ZONE 'UTC')::date AS day,
               COUNT(*)  AS signals,
               COUNT(*) FILTER (WHERE outcome = 1) AS wins,
               COUNT(*) FILTER (WHERE outcome IS NOT NULL) AS resolved
        FROM prop_scan_log
        GROUP BY day
        ORDER BY day DESC
        LIMIT 14
        """
    )

    out_dir = _ensure(_OBSIDIAN_ROOT / "P&L")
    out_file = out_dir / "tracker.md"

    resolved = int(overall["resolved"] or 0)
    wins = int(overall["wins"] or 0)
    win_rate = wins / resolved * 100 if resolved else 0.0
    total_pnl = float(overall["total_pnl"] or 0.0)
    avg_roi = float(overall["avg_roi"] or 0.0)

    lines = [
        "---",
        "type: pnl_tracker",
        f"updated: {now_str}",
        "---",
        "",
        "# Prop Scanner P&L Tracker",
        "",
        f"*Last updated: {now_str}*",
        "",
        "## Overall Stats",
        "",
        f"- Signals logged: **{overall['total']}**",
        f"- Resolved: **{resolved}** | Win rate: **{win_rate:.1f}%**",
        f"- Avg ROI at signal: **{avg_roi:+.1f}%**",
        f"- Simulated P&L (per $1 position): **{total_pnl:+.2f}$**",
        "",
        "> P&L is simulated at $1 per signal, YES side, taker fee 3%.",
        "",
        "## By Prop Type",
        "",
        "| Type | Signals | Resolved | Win rate | Avg ROI | P&L ($1/signal) |",
        "|------|---------|----------|----------|---------|-----------------|",
    ]

    for r in by_type:
        res = int(r["resolved"] or 0)
        w = int(r["wins"] or 0)
        wr = w / res * 100 if res else 0.0
        pnl = float(r["total_pnl"] or 0.0)
        lines.append(
            f"| {r['prop_type']} "
            f"| {r['total']} "
            f"| {res} "
            f"| {wr:.1f}% "
            f"| {float(r['avg_roi'] or 0):+.1f}% "
            f"| {pnl:+.2f} |"
        )

    lines += [
        "",
        "## Last 14 Days",
        "",
        "| Date | Signals | Resolved | Wins |",
        "|------|---------|----------|------|",
    ]

    for r in recent:
        lines.append(
            f"| {r['day']} "
            f"| {r['signals']} "
            f"| {r['resolved']} "
            f"| {r['wins']} |"
        )

    lines += [
        "",
        "## Notes",
        "",
        "- Outcome 1 = YES won, 0 = NO won, — = not yet resolved.",
        "- Outcomes are auto-resolved by the daemon via Gamma API 3h after game start.",
        "- Run `make obsidian` to refresh this file.",
        "",
    ]

    out_file.write_text("\n".join(lines), encoding="utf-8")
    return out_file


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def generate_all(config: dict) -> None:
    """Generate all Obsidian reports. Intended for calling from other modules (e.g. prop_scanner daemon)."""
    pool = await _pool(config)
    try:
        await report_daily(pool, date.today())
        await report_pnl(pool)
        await report_calibration(pool)
    finally:
        await pool.close()


async def main(config: dict, report: str | None, target_date: date) -> None:
    pool = await _pool(config)
    try:
        reports_to_run = {report} if report else {"daily", "pnl", "calibration"}

        if "daily" in reports_to_run:
            path = await report_daily(pool, target_date)
            print(f"  Daily log  → {path.relative_to(_PROJECT_ROOT)}")

        if "pnl" in reports_to_run:
            path = await report_pnl(pool)
            print(f"  P&L        → {path.relative_to(_PROJECT_ROOT)}")

        if "calibration" in reports_to_run:
            path = await report_calibration(pool)
            print(f"  Calibration→ {path.relative_to(_PROJECT_ROOT)}")

    finally:
        await pool.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate Obsidian markdown reports from prop scanner DB")
    p.add_argument(
        "--report",
        choices=["daily", "pnl", "calibration"],
        default=None,
        help="Which report to generate (default: all)",
    )
    p.add_argument(
        "--date",
        default=None,
        help="Date for daily report in YYYY-MM-DD format (default: today)",
    )
    p.add_argument("--config", default="config/settings.yaml")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: {args.config} not found.")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)

    target_date = date.fromisoformat(args.date) if args.date else date.today()

    try:
        asyncio.run(main(config, args.report, target_date))
    except KeyboardInterrupt:
        print("\nStopped.")
