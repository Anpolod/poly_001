"""
Dashboard Data Exporter — queries PostgreSQL and writes dashboard_data.json.

Run this on a schedule (cron every 1-5 min) or call from bot_main.
The HTML dashboard reads the JSON file and renders charts + tables.

Usage:
    python dashboard/export_dashboard_data.py
    python dashboard/export_dashboard_data.py --config config/settings.yaml
    python dashboard/export_dashboard_data.py --watch   # refresh every 60s
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_OUTPUT_PATH = Path(__file__).resolve().parent / "dashboard_data.json"


def _detect_sport(slug: str, question: str, signal_type: str) -> str:
    """Infer sport from market slug/question/signal_type."""
    text = f"{slug} {question} {signal_type}".lower()
    if any(k in text for k in ("nba", "basketball", "tanking")):
        return "nba"
    if any(k in text for k in ("mlb", "baseball", "pitcher")):
        return "mlb"
    if any(k in text for k in ("nhl", "hockey")):
        return "hockey"
    if any(k in text for k in ("soccer", "football", "epl", "la liga", "serie a", "bundesliga", "ucl")):
        return "football"
    return "other"


async def export(config: dict) -> dict:
    """Query DB and build the full dashboard data dict."""
    db = config["database"]
    pool = await asyncpg.create_pool(
        host=db["host"], port=db["port"],
        database=db["name"], user=db["user"],
        password=str(db["password"]),
        min_size=1, max_size=3,
    )

    now = datetime.now(tz=timezone.utc)
    data = {}

    try:
        # ── Bot state ──
        bot_state_path = _PROJECT_ROOT / "logs" / "bot_state.json"
        balance = 0.0
        bot_status = "unknown"
        if bot_state_path.exists():
            try:
                bs = json.loads(bot_state_path.read_text())
                balance = bs.get("balance_usd", 0.0)
                updated = bs.get("updated_at", "")
                trading_on = bs.get("trading_enabled", False)
                bot_status = f"{'LIVE' if trading_on else 'DRY RUN'} · last ping {updated[:16]}"
            except Exception:
                pass

        # ── Open positions ──
        open_rows = await pool.fetch("""
            SELECT op.market_id, op.slug, op.signal_type, op.side,
                   op.entry_price, op.size_usd, op.current_bid, op.game_start,
                   m.question
            FROM open_positions op
            LEFT JOIN markets m ON m.id = op.market_id
            WHERE op.status = 'open'
            ORDER BY op.entry_ts DESC
        """)

        positions = []
        total_exposure = 0.0
        for r in open_rows:
            sport = _detect_sport(r["slug"] or "", r["question"] or "", r["signal_type"] or "")
            hours_left = None
            if r["game_start"]:
                gs = r["game_start"]
                if gs.tzinfo is None:
                    gs = gs.replace(tzinfo=timezone.utc)
                hours_left = max(0, (gs - now).total_seconds() / 3600)

            positions.append({
                "sport": sport,
                "match": (r["question"] or r["slug"] or "")[:60],
                "strategy": r["signal_type"] or "—",
                "entry": float(r["entry_price"] or 0),
                "current_bid": float(r["current_bid"] or r["entry_price"] or 0),
                "size_usd": float(r["size_usd"] or 0),
                "hours_left": round(hours_left, 1) if hours_left is not None else None,
            })
            total_exposure += float(r["size_usd"] or 0)

        # ── Closed positions (last 30 days) ──
        closed_rows = await pool.fetch("""
            SELECT op.market_id, op.slug, op.signal_type,
                   op.entry_price, op.exit_price, op.pnl_usd,
                   op.entry_ts, op.exit_ts, op.size_usd,
                   m.question
            FROM open_positions op
            LEFT JOIN markets m ON m.id = op.market_id
            WHERE op.status = 'closed'
              AND op.exit_ts > NOW() - INTERVAL '30 days'
            ORDER BY op.exit_ts DESC
        """)

        closed = []
        pnl_history = []
        sport_stats = {}  # sport -> {wins, losses, pnl, trades}

        for r in closed_rows:
            sport = _detect_sport(r["slug"] or "", r["question"] or "", r["signal_type"] or "")
            entry = float(r["entry_price"] or 0)
            exit_p = float(r["exit_price"] or 0)
            pnl = float(r["pnl_usd"] or 0)
            exit_date = (r["exit_ts"] or r["entry_ts"] or now).strftime("%Y-%m-%d")

            closed.append({
                "date": exit_date,
                "sport": sport,
                "match": (r["question"] or r["slug"] or "")[:60],
                "strategy": r["signal_type"] or "—",
                "entry": entry,
                "exit": exit_p,
                "pnl": pnl,
            })

            pnl_history.append({
                "date": exit_date,
                "sport": sport,
                "pnl": pnl,
            })

            if sport not in sport_stats:
                sport_stats[sport] = {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0}
            ss = sport_stats[sport]
            ss["trades"] += 1
            ss["pnl"] += pnl
            if pnl > 0:
                ss["wins"] += 1
            elif pnl < 0:
                ss["losses"] += 1

        total_wins = sum(s["wins"] for s in sport_stats.values())
        total_losses = sum(s["losses"] for s in sport_stats.values())
        total_trades = sum(s["trades"] for s in sport_stats.values())
        total_pnl = sum(s["pnl"] for s in sport_stats.values())
        win_rate = (total_wins / (total_wins + total_losses) * 100) if (total_wins + total_losses) > 0 else 0

        # ── Compute period change ──
        week_ago = now - timedelta(days=7)
        week_pnl = sum(
            h["pnl"] for h in pnl_history
            if h["date"] >= week_ago.strftime("%Y-%m-%d")
        )
        pnl_period = f"{'↑' if week_pnl >= 0 else '↓'} ${abs(week_pnl):.2f} this week"

        # ── Format sport stats ──
        formatted_sports = {}
        for sport_key in ["nba", "mlb", "football", "hockey"]:
            ss = sport_stats.get(sport_key, {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0})
            wr = (ss["wins"] / (ss["wins"] + ss["losses"]) * 100) if (ss["wins"] + ss["losses"]) > 0 else 0
            formatted_sports[sport_key] = {
                "wins": ss["wins"],
                "losses": ss["losses"],
                "trades": ss["trades"],
                "pnl": round(ss["pnl"], 2),
                "win_rate": round(wr, 1),
            }

        # ── Active signals: tanking ──
        signals = []
        tanking_rows = await pool.fetch("""
            SELECT market_id, motivated_team, tanking_team,
                   current_price, drift_24h, pattern_strength, action, game_start
            FROM tanking_signals
            WHERE scanned_at > NOW() - INTERVAL '12 hours'
              AND game_start > NOW()
            ORDER BY scanned_at DESC
            LIMIT 20
        """)
        for r in tanking_rows:
            hours_to = None
            if r["game_start"]:
                gs = r["game_start"]
                if gs.tzinfo is None:
                    gs = gs.replace(tzinfo=timezone.utc)
                hours_to = max(0, (gs - now).total_seconds() / 3600)
            signals.append({
                "sport": "nba",
                "strength": r["pattern_strength"] or "WATCH",
                "match": f"{r['motivated_team'] or '?'} vs {r['tanking_team'] or '?'}",
                "type": "Tanking",
                "price": float(r["current_price"] or 0),
                "drift": float(r["drift_24h"]) if r["drift_24h"] is not None else None,
                "action": r["action"] or "WATCH",
                "hours_to_game": round(hours_to, 1) if hours_to is not None else None,
            })

        # ── Active signals: pitcher ──
        pitcher_rows = await pool.fetch("""
            SELECT market_id, favored_team, underdog_team,
                   home_pitcher, home_era, away_pitcher, away_era,
                   current_price, drift_24h, signal_strength, action, game_start
            FROM pitcher_signals
            WHERE scanned_at > NOW() - INTERVAL '12 hours'
              AND game_start > NOW()
            ORDER BY scanned_at DESC
            LIMIT 20
        """)
        for r in pitcher_rows:
            hours_to = None
            if r["game_start"]:
                gs = r["game_start"]
                if gs.tzinfo is None:
                    gs = gs.replace(tzinfo=timezone.utc)
                hours_to = max(0, (gs - now).total_seconds() / 3600)
            h_era = f" ({r['home_era']:.1f})" if r["home_era"] else ""
            a_era = f" ({r['away_era']:.1f})" if r["away_era"] else ""
            signals.append({
                "sport": "mlb",
                "strength": r["signal_strength"] or "WATCH",
                "match": f"{r['favored_team'] or '?'} vs {r['underdog_team'] or '?'}",
                "type": f"Pitcher{h_era} vs{a_era}",
                "price": float(r["current_price"] or 0),
                "drift": float(r["drift_24h"]) if r["drift_24h"] is not None else None,
                "action": r["action"] or "WATCH",
                "hours_to_game": round(hours_to, 1) if hours_to is not None else None,
            })

        # Sort signals: HIGH first
        rank = {"HIGH": 3, "MODERATE": 2, "WATCH": 1}
        signals.sort(key=lambda s: -rank.get(s["strength"], 0))

        # ── Assemble ──
        data = {
            "updated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
            "bot_status": bot_status,
            "kpi": {
                "total_pnl": round(total_pnl, 2),
                "pnl_period": pnl_period,
                "win_rate": round(win_rate, 1),
                "wins": total_wins,
                "losses": total_losses,
                "breakeven": total_trades - total_wins - total_losses,
                "open_positions": len(positions),
                "exposure": round(total_exposure, 2),
                "balance": round(balance, 2),
            },
            "sports": formatted_sports,
            "pnl_history": pnl_history,
            "signals": signals,
            "positions": positions,
            "closed": closed[:50],
        }

    finally:
        await pool.close()

    return data


async def run(config: dict, watch: bool = False) -> None:
    while True:
        try:
            data = await export(config)
            _OUTPUT_PATH.write_text(json.dumps(data, indent=2, default=str))
            logger.info("Exported dashboard data → %s (%d signals, %d positions, %d closed)",
                        _OUTPUT_PATH.name,
                        len(data.get("signals", [])),
                        len(data.get("positions", [])),
                        len(data.get("closed", [])))
        except Exception as exc:
            logger.error("Export failed: %s", exc)

        if not watch:
            break
        await asyncio.sleep(60)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Export dashboard data from DB to JSON")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--watch", action="store_true", help="Re-export every 60s")
    args = p.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        # Try relative to project root
        config_path = _PROJECT_ROOT / args.config
    if not config_path.exists():
        print(f"ERROR: {args.config} not found")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    asyncio.run(run(config, watch=args.watch))
