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
_OBSIDIAN_ROOT = _PROJECT_ROOT / "obsidian"


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
        lines += [
            f"### {mtype.title()}",
            "",
            "| Bucket | N | Avg price | Actual win | Edge (pp) |",
            "|--------|---|-----------|------------|-----------|",
        ]
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
# Report: Trading diary (open_positions table)
# ---------------------------------------------------------------------------


async def report_trading(pool: asyncpg.Pool, target_date: date) -> Path:
    """Write obsidian/Trading/YYYY-MM-DD.md — daily trade diary."""
    date_str = target_date.isoformat()
    day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)

    rows = await pool.fetch(
        """
        SELECT slug, signal_type, side, size_usd, size_shares,
               entry_price, exit_price, entry_ts, exit_ts, pnl_usd, status,
               clob_order_id, notes
        FROM open_positions
        WHERE entry_ts >= $1 AND entry_ts < $1 + INTERVAL '1 day'
        ORDER BY entry_ts
        """,
        day_start,
    )

    out_dir = _ensure(_OBSIDIAN_ROOT / "Trading")
    out_file = out_dir / f"{date_str}.md"

    total_pnl = sum(float(r["pnl_usd"] or 0) for r in rows if r["status"] == "closed")
    closed = [r for r in rows if r["status"] == "closed"]
    wins = sum(1 for r in closed if float(r["pnl_usd"] or 0) >= 0)

    lines = [
        "---",
        f"date: {date_str}",
        "type: trading_diary",
        f"trades: {len(rows)}",
        f"closed: {len(closed)}",
        f"total_pnl: {total_pnl:.2f}",
        "---",
        "",
        f"# Trading Diary — {date_str}",
        "",
    ]

    if not rows:
        lines += ["*No trades entered today.*", ""]
    else:
        lines += [
            "## Entries",
            "",
            "| Market | Signal | Status | Entry$ | Exit$ | Size USD | P&L | Notes |",
            "|--------|--------|--------|--------|-------|----------|-----|-------|",
        ]
        for r in rows:
            status_badge = {"open": "⏳", "closed": "✅", "cancelled": "❌"}.get(r["status"], "?")
            pnl_str = f"{float(r['pnl_usd']):+.2f}" if r["pnl_usd"] is not None else "—"
            exit_str = f"{float(r['exit_price']):.3f}" if r["exit_price"] else "—"
            lines.append(
                f"| {(r['slug'] or '')[:35]} "
                f"| {r['signal_type'] or '—'} "
                f"| {status_badge} {r['status']} "
                f"| {float(r['entry_price']):.3f} "
                f"| {exit_str} "
                f"| ${float(r['size_usd']):.2f} "
                f"| {pnl_str} "
                f"| {(r['notes'] or '')[:50]} |"
            )
        lines.append("")

        if closed:
            wr = wins / len(closed) * 100
            lines += [
                "## Day Summary",
                "",
                f"- Trades entered: **{len(rows)}**",
                f"- Closed today: **{len(closed)}**  (win rate {wr:.0f}%)",
                f"- Total P&L: **${total_pnl:+.2f}**",
                "",
            ]

    out_file.write_text("\n".join(lines), encoding="utf-8")
    return out_file


async def report_trading_summary(pool: asyncpg.Pool) -> Path:
    """Write obsidian/Trading/summary.md — all-time trading stats."""
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    overall = await pool.fetchrow(
        """
        SELECT
            COUNT(*)                                          AS total,
            COUNT(*) FILTER (WHERE status = 'closed')        AS closed,
            COUNT(*) FILTER (WHERE status = 'open')          AS open_count,
            COUNT(*) FILTER (WHERE status = 'cancelled')     AS cancelled,
            COUNT(*) FILTER (WHERE pnl_usd > 0)              AS wins,
            COALESCE(SUM(pnl_usd) FILTER (WHERE status = 'closed'), 0) AS total_pnl,
            COALESCE(AVG(pnl_usd) FILTER (WHERE status = 'closed'), 0) AS avg_pnl,
            COALESCE(MAX(pnl_usd), 0)                        AS best_trade,
            COALESCE(MIN(pnl_usd), 0)                        AS worst_trade,
            COALESCE(SUM(size_usd) FILTER (WHERE status = 'open'), 0) AS open_exposure
        FROM open_positions
        """
    )

    by_signal = await pool.fetch(
        """
        SELECT signal_type,
               COUNT(*)                                       AS total,
               COUNT(*) FILTER (WHERE status = 'closed')     AS closed,
               COUNT(*) FILTER (WHERE pnl_usd > 0)           AS wins,
               COALESCE(SUM(pnl_usd) FILTER (WHERE status='closed'), 0) AS total_pnl
        FROM open_positions
        GROUP BY signal_type
        ORDER BY signal_type
        """
    )

    recent = await pool.fetch(
        """
        SELECT DATE(entry_ts AT TIME ZONE 'UTC') AS day,
               COUNT(*)                          AS trades,
               COUNT(*) FILTER (WHERE status='closed') AS closed,
               COALESCE(SUM(pnl_usd) FILTER (WHERE status='closed'), 0) AS pnl
        FROM open_positions
        WHERE entry_ts >= NOW() - INTERVAL '14 days'
        GROUP BY day
        ORDER BY day DESC
        """
    )

    out_dir = _ensure(_OBSIDIAN_ROOT / "Trading")
    out_file = out_dir / "summary.md"

    total = int(overall["total"] or 0)
    closed = int(overall["closed"] or 0)
    wins = int(overall["wins"] or 0)
    wr = wins / closed * 100 if closed else 0.0
    total_pnl = float(overall["total_pnl"] or 0)

    lines = [
        "---",
        "type: trading_summary",
        f"updated: {now_str}",
        f"total_trades: {total}",
        f"total_pnl: {total_pnl:.2f}",
        "---",
        "",
        "# Trading Summary",
        f"*Updated: {now_str}*",
        "",
        "## All-Time Stats",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total trades | {total} |",
        f"| Closed | {closed} |",
        f"| Open | {int(overall['open_count'] or 0)} |",
        f"| Cancelled | {int(overall['cancelled'] or 0)} |",
        f"| Win rate | {wr:.1f}% ({wins}/{closed}) |",
        f"| Total P&L | **${total_pnl:+.2f}** |",
        f"| Avg P&L/trade | ${float(overall['avg_pnl'] or 0):+.2f} |",
        f"| Best trade | ${float(overall['best_trade'] or 0):+.2f} |",
        f"| Worst trade | ${float(overall['worst_trade'] or 0):+.2f} |",
        f"| Open exposure | ${float(overall['open_exposure'] or 0):.2f} |",
        "",
        "## By Signal Type",
        "",
        "| Signal | Total | Closed | Wins | P&L |",
        "|--------|-------|--------|------|-----|",
    ]

    for r in by_signal:
        cl = int(r["closed"] or 0)
        w = int(r["wins"] or 0)
        wr_s = f"{w/cl*100:.0f}%" if cl else "—"
        lines.append(
            f"| {r['signal_type'] or '—'} "
            f"| {r['total']} "
            f"| {cl} "
            f"| {wr_s} "
            f"| ${float(r['total_pnl'] or 0):+.2f} |"
        )

    lines += ["", "## Last 14 Days", "", "| Date | Trades | Closed | P&L |", "|------|--------|--------|-----|"]
    for r in recent:
        lines.append(
            f"| {r['day']} "
            f"| {r['trades']} "
            f"| {r['closed']} "
            f"| ${float(r['pnl'] or 0):+.2f} |"
        )

    lines += ["", "---", "*Run `make obsidian-trading` to refresh.*", ""]
    out_file.write_text("\n".join(lines), encoding="utf-8")
    return out_file


# ---------------------------------------------------------------------------
# Public helper — call from bot_main after closing a position
# ---------------------------------------------------------------------------


async def log_closed_trade(config: dict, position_id: int) -> None:
    """Refresh trading diary for the day a position was entered. Fire-and-forget."""
    try:
        pool = await _pool(config)
        try:
            row = await pool.fetchrow(
                "SELECT entry_ts FROM open_positions WHERE id=$1", position_id
            )
            trade_date = row["entry_ts"].date() if row else date.today()
            await report_trading(pool, trade_date)
            await report_trading_summary(pool)
        finally:
            await pool.close()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Obsidian trade log failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def main(config: dict, report: str | None, target_date: date) -> None:
    pool = await _pool(config)
    try:
        reports_to_run = {report} if report else {"daily", "pnl", "calibration", "trading"}

        if "daily" in reports_to_run:
            path = await report_daily(pool, target_date)
            print(f"  Daily log  → {path.relative_to(_PROJECT_ROOT)}")

        if "pnl" in reports_to_run:
            path = await report_pnl(pool)
            print(f"  P&L        → {path.relative_to(_PROJECT_ROOT)}")

        if "calibration" in reports_to_run:
            path = await report_calibration(pool)
            print(f"  Calibration→ {path.relative_to(_PROJECT_ROOT)}")

        if "trading" in reports_to_run:
            path = await report_trading(pool, target_date)
            print(f"  Trading    → {path.relative_to(_PROJECT_ROOT)}")
            path = await report_trading_summary(pool)
            print(f"  Summary    → {path.relative_to(_PROJECT_ROOT)}")

    finally:
        await pool.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate Obsidian markdown reports from prop scanner DB")
    p.add_argument(
        "--report",
        choices=["daily", "pnl", "calibration", "trading"],
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
