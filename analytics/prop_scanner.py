"""
Player Prop Scanner

Scans live Polymarket sports events for player prop markets (points/rebounds/assists)
in the 30–60% price range, cross-references against the historical calibration model,
and displays ranked opportunities with expected value and order book depth.

Run this 6–12 hours before tip-off for maximum edge capture.

Usage:
    python -m analytics.prop_scanner
    python -m analytics.prop_scanner --min-ev 0.05      # only show ROI > 5%
    python -m analytics.prop_scanner --prop-types points rebounds assists
    python -m analytics.prop_scanner --price-min 0.25 --price-max 0.55
    python -m analytics.prop_scanner --watch              # refresh every 5 min
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import asyncpg
import yaml

# ---------------------------------------------------------------------------
# DB helpers (daemon mode)
# ---------------------------------------------------------------------------

_DB_POOL: asyncpg.Pool | None = None


async def _get_pool(config: dict) -> asyncpg.Pool:
    global _DB_POOL
    if _DB_POOL is None:
        db = config["database"]
        _DB_POOL = await asyncpg.create_pool(
            host=db["host"],
            port=db["port"],
            database=db["name"],
            user=db["user"],
            password=str(db["password"]),
            min_size=1,
            max_size=3,
        )
    return _DB_POOL

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# From historical calibration (3-month dataset, 3500+ markets per type)
# Keys: market_type → (avg_actual_win_rate, avg_close_price, model_edge_pp)
_CALIBRATION_MODEL: dict[str, tuple[float, float, float]] = {
    "points":   (0.462, 0.410, +5.2),
    "rebounds": (0.438, 0.398, +4.0),
    "assists":  (0.459, 0.404, +5.5),
}

# Polymarket taker fee (3% of payout at resolution)
_TAKER_FEE = 0.03

_DEFAULT_PROP_TYPES = ["points", "rebounds", "assists"]
_REFRESH_INTERVAL = 300   # seconds


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PropOpportunity:
    market_id: str
    slug: str
    prop_type: str          # points | rebounds | assists
    player_name: str
    threshold: str          # e.g. "23.5"
    game_slug: str
    hours_until_game: float
    yes_price: float        # current mid / last trade price
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_depth_usd: float    # $ at best bid
    ask_depth_usd: float    # $ at best ask
    # Model-derived
    model_win_rate: float   # calibration-adjusted expected win probability
    ev_per_unit: float      # expected profit per $1 bought
    roi_pct: float          # ev / price * 100


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


async def _fetch_active_events(
    session: aiohttp.ClientSession, gamma_url: str
) -> list[dict]:
    try:
        async with session.get(
            f"{gamma_url}/events",
            params={"tag": "sports", "active": "true", "closed": "false", "limit": 500},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            return await r.json()
    except Exception as exc:
        logger.warning(f"Failed to fetch events: {exc}")
        return []


async def _fetch_orderbook(
    session: aiohttp.ClientSession, clob_url: str, token_id: str
) -> dict:
    try:
        async with session.get(
            f"{clob_url}/book",
            params={"token_id": token_id},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status == 200:
                return await r.json()
    except Exception:
        pass
    return {}


def _extract_player_info(slug: str) -> tuple[str, str]:
    """
    Parse player name and threshold from slug.
    e.g. 'nba-bos-lal-2026-02-22-points-jayson-tatum-27pt5'
    → ('jayson tatum', '27.5')
    """
    parts = slug.split("-")
    # Find prop type position
    prop_types = {"points", "rebounds", "assists"}
    try:
        idx = next(i for i, p in enumerate(parts) if p in prop_types)
    except StopIteration:
        return slug, "?"

    name_parts = parts[idx + 1: -1]
    threshold_raw = parts[-1]  # e.g. "27pt5"
    player_name = " ".join(name_parts).title()
    threshold = threshold_raw.replace("pt", ".").replace("p", ".")

    return player_name, threshold


def _compute_ev(yes_price: float, model_win_rate: float) -> tuple[float, float]:
    """
    EV per $1 position: win * (0.97 - price) - lose * price
    Returns (ev_per_unit, roi_pct).
    """
    ev = model_win_rate * (1 - _TAKER_FEE - yes_price) - (1 - model_win_rate) * yes_price
    roi = ev / yes_price * 100 if yes_price > 0 else 0.0
    return round(ev, 4), round(roi, 1)


def _estimate_win_rate(market_type: str, yes_price: float) -> float:
    """
    Estimate true win probability using the calibration model.
    The model shows that YES wins ~5pp more than the price suggests
    for props priced 30–50%, less so outside that range.

    Simple linear correction: add the per-bucket adjustment from the
    calibration data rather than a flat adjustment.
    """
    base_actual, base_price, _ = _CALIBRATION_MODEL.get(
        market_type, (0.46, 0.41, 5.0)
    )

    # The model shows the adjustment varies by price bucket
    # (from calibration: ~9-15pp for 30-40%, ~4-5pp for 40-50%, ~2pp for 50-60%)
    if yes_price < 0.30:
        adj = 0.09       # extrapolated from <30% bucket
    elif yes_price < 0.40:
        adj = 0.12       # 30-40% bucket average
    elif yes_price < 0.50:
        adj = 0.04       # 40-50% bucket average
    elif yes_price < 0.60:
        adj = 0.02       # 50-60% bucket average
    else:
        adj = -0.06      # >=60% bucket is overpriced

    # Clamp to valid probability range
    return min(max(yes_price + adj, 0.01), 0.99)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


async def scan(
    config: dict,
    prop_types: list[str],
    price_min: float,
    price_max: float,
    min_ev: float,
    hours_window: float,
) -> list[PropOpportunity]:
    gamma_url = config["api"]["gamma_base_url"]
    clob_url = config["api"]["clob_base_url"]
    now = datetime.now(tz=timezone.utc)

    opportunities: list[PropOpportunity] = []

    async with aiohttp.ClientSession() as session:
        events = await _fetch_active_events(session, gamma_url)
        if not events:
            return []

        # Collect all matching prop markets
        candidates: list[tuple[dict, dict]] = []   # (market, event)
        for event in events:
            mkts_raw = event.get("markets", [])
            mkts = json.loads(mkts_raw) if isinstance(mkts_raw, str) else mkts_raw
            for m in mkts:
                if not isinstance(m, dict):
                    continue
                if m.get("sportsMarketType") not in prop_types:
                    continue
                gs = m.get("gameStartTime", "")
                if not gs:
                    continue
                try:
                    game_dt = datetime.fromisoformat(
                        gs.replace(" ", "T").replace("+00", "+00:00")
                    )
                except ValueError:
                    continue

                hours_until = (game_dt - now).total_seconds() / 3600
                if hours_until < 0 or hours_until > hours_window:
                    continue

                ltp = m.get("lastTradePrice")
                if ltp is None:
                    continue
                try:
                    yes_price = float(ltp)
                except (TypeError, ValueError):
                    continue

                if not (price_min <= yes_price <= price_max):
                    continue

                candidates.append((m, event))

        # Fetch orderbooks concurrently (max 15 parallel to avoid rate-limiting)
        _sem = asyncio.Semaphore(15)

        async def _enrich(m: dict, _event: dict) -> Optional[PropOpportunity]:
            token_raw = m.get("clobTokenIds", "[]")
            tokens = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
            if not tokens:
                return None

            async with _sem:
                book = await _fetch_orderbook(session, clob_url, tokens[0])
            bids = book.get("bids", [])
            asks = book.get("asks", [])

            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            bid_depth = float(bids[0]["size"]) * (best_bid or 0) if bids else 0.0
            ask_depth = float(asks[0]["size"]) * (best_ask or 0) if asks else 0.0

            # Use mid price if available, else last trade price
            if best_bid and best_ask:
                yes_price = (best_bid + best_ask) / 2
            else:
                ltp = m.get("lastTradePrice")
                yes_price = float(ltp) if ltp else 0.0

            if not (price_min <= yes_price <= price_max):
                return None

            prop_type = m.get("sportsMarketType", "")
            slug = m.get("slug", "")
            player_name, threshold = _extract_player_info(slug)

            gs = m.get("gameStartTime", "")
            game_dt = datetime.fromisoformat(gs.replace(" ", "T").replace("+00", "+00:00"))
            hours_until = (game_dt - now).total_seconds() / 3600

            model_win = _estimate_win_rate(prop_type, yes_price)
            ev, roi = _compute_ev(yes_price, model_win)

            if roi < min_ev * 100:
                return None

            # Extract game slug (drop player portion)
            parts = slug.split("-")
            prop_idx = next(
                (i for i, p in enumerate(parts) if p in {"points", "rebounds", "assists"}),
                len(parts),
            )
            game_slug = "-".join(parts[:prop_idx])

            return PropOpportunity(
                market_id=str(m.get("id", "")),
                slug=slug,
                prop_type=prop_type,
                player_name=player_name,
                threshold=threshold,
                game_slug=game_slug,
                hours_until_game=round(hours_until, 1),
                yes_price=round(yes_price, 3),
                best_bid=best_bid,
                best_ask=best_ask,
                bid_depth_usd=round(bid_depth, 1),
                ask_depth_usd=round(ask_depth, 1),
                model_win_rate=round(model_win, 3),
                ev_per_unit=ev,
                roi_pct=roi,
            )

        tasks = [asyncio.create_task(_enrich(m, ev)) for m, ev in candidates]
        results = await asyncio.gather(*tasks)
        opportunities = [r for r in results if r is not None]

    return sorted(opportunities, key=lambda x: -x.roi_pct)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def _print_opportunities(opps: list[PropOpportunity], price_window: str) -> None:
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'='*110}")
    print(f"  PLAYER PROP SCANNER  —  {now_str}")
    print("  Model: historical calibration on 10,800 NBA prop markets (Feb–Apr 2026)")
    print(f"{'='*110}")

    if not opps:
        print("\n  No opportunities match current filters.\n")
        print("  Tip: run 6–12h before tip-off when props have most edge.")
        print(f"{'='*110}\n")
        return

    header = (
        f"{'Player':<22} {'Prop':<10} {'Thresh':>7}  "
        f"{'Game':>5}h  {'Price':>6}  {'Bid':>6}  {'Ask':>6}  "
        f"{'BidSz$':>7}  {'AskSz$':>7}  "
        f"{'ModelWin':>9}  {'EV/unit':>8}  {'ROI%':>6}"
    )
    print(f"\n  {header}")
    print("  " + "─" * 108)

    for o in opps:
        bid_str = f"{o.best_bid:.3f}" if o.best_bid else "  —  "
        ask_str = f"{o.best_ask:.3f}" if o.best_ask else "  —  "
        spread_ok = (
            o.best_ask is not None and o.best_bid is not None
            and (o.best_ask - o.best_bid) <= 0.04
        )
        flag = "✓" if spread_ok else " "

        row = (
            f"{o.player_name:<22} {o.prop_type:<10} {o.threshold:>7}  "
            f"{o.hours_until_game:>5.1f}h  {o.yes_price:>6.3f}  "
            f"{bid_str:>6}  {ask_str:>6}  "
            f"{o.bid_depth_usd:>7.0f}  {o.ask_depth_usd:>7.0f}  "
            f"{o.model_win_rate:>9.3f}  {o.ev_per_unit:>+8.4f}  {o.roi_pct:>+5.1f}% {flag}"
        )
        print(f"  {row}")

    print()
    # Summary by prop type
    for pt in set(o.prop_type for o in opps):
        pt_opps = [o for o in opps if o.prop_type == pt]
        avg_roi = sum(o.roi_pct for o in pt_opps) / len(pt_opps)
        print(f"  [{pt.upper()}] {len(pt_opps)} opportunities | avg ROI {avg_roi:+.1f}%")

    print()
    print("  ✓ = spread ≤ 4¢ (reasonable to fill)  |  EV/unit = expected profit per $1 at YES price")
    print("  Model win rate = historical actual win rate adjusted for current price bucket")
    print(f"{'='*110}\n")


# ---------------------------------------------------------------------------
# Daemon helpers
# ---------------------------------------------------------------------------


async def log_to_db(
    pool: asyncpg.Pool,
    opportunities: list[PropOpportunity],
    alert_min_roi: float = 5.0,
    alert_min_depth_usd: float = 50.0,
) -> list[PropOpportunity]:
    """Insert new opportunities into prop_scan_log; skip if already logged today.

    Returns the list of newly-inserted opportunities that meet the alert thresholds,
    so the caller can send Slack alerts only for genuinely new signals.
    """
    if not opportunities:
        return []

    newly_logged: list[PropOpportunity] = []

    async with pool.acquire() as conn:
        for opp in opportunities:
            # Deduplication: skip if this market already logged in the last 24 h
            existing = await conn.fetchval(
                """
                SELECT id FROM prop_scan_log
                WHERE market_id = $1
                  AND scanned_at > NOW() - INTERVAL '24 hours'
                LIMIT 1
                """,
                opp.market_id,
            )
            if existing:
                continue

            # Resolve game_start from hours_until_game
            game_start = datetime.now(tz=timezone.utc) + timedelta(hours=opp.hours_until_game)

            await conn.execute(
                """
                INSERT INTO prop_scan_log
                    (market_id, slug, prop_type, player_name, threshold,
                     game_start, hours_until, yes_price, model_win,
                     ev_per_unit, roi_pct, bid_depth, ask_depth)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """,
                opp.market_id,
                opp.slug,
                opp.prop_type,
                opp.player_name,
                opp.threshold,
                game_start,
                opp.hours_until_game,
                opp.yes_price,
                opp.model_win_rate,
                opp.ev_per_unit,
                opp.roi_pct,
                opp.bid_depth_usd,
                opp.ask_depth_usd,
            )

            if opp.roi_pct >= alert_min_roi and opp.ask_depth_usd >= alert_min_depth_usd:
                newly_logged.append(opp)

    return newly_logged


async def resolve_outcomes(pool: asyncpg.Pool, gamma_url: str) -> int:
    """Check prop_scan_log rows where game has ended but outcome is still NULL.

    Fetches outcomePrices from the Gamma API and updates outcome (1=YES, 0=NO).
    Returns the number of rows resolved.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, market_id
            FROM prop_scan_log
            WHERE outcome IS NULL
              AND game_start < NOW() - INTERVAL '3 hours'
            """
        )

    if not rows:
        return 0

    resolved = 0
    async with aiohttp.ClientSession() as session:
        for row in rows:
            try:
                async with session.get(
                    f"{gamma_url}/markets/{row['market_id']}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()

                outcome_prices_raw = data.get("outcomePrices")
                if not outcome_prices_raw:
                    continue
                prices = (
                    outcome_prices_raw
                    if isinstance(outcome_prices_raw, list)
                    else json.loads(outcome_prices_raw)
                )
                if len(prices) < 2:
                    continue

                yes_p = float(prices[0])
                outcome = 1 if yes_p > 0.5 else 0

                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE prop_scan_log
                        SET outcome = $1, resolved_at = NOW()
                        WHERE id = $2
                        """,
                        outcome,
                        row["id"],
                    )
                resolved += 1

            except Exception as exc:
                logger.warning(f"resolve_outcomes failed for {row['market_id']}: {exc}")

    return resolved


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run(
    config: dict,
    prop_types: list[str],
    price_min: float,
    price_max: float,
    min_ev: float,
    hours_window: float,
    watch: bool,
    daemon: bool = False,
    once: bool = False,
) -> None:
    """Main run loop.

    - watch: print table every _REFRESH_INTERVAL seconds (no DB)
    - daemon: persist to DB, send alerts for new signals, resolve outcomes
    - daemon + once: run a single daemon cycle (useful for testing)
    """
    scanner_cfg = config.get("prop_scanner", {})
    scan_interval = scanner_cfg.get("scan_interval_sec", _REFRESH_INTERVAL)
    alert_min_roi = scanner_cfg.get("alert_min_roi", 5.0)
    alert_min_depth = scanner_cfg.get("alert_min_depth_usd", 50.0)
    gamma_url = config["api"]["gamma_base_url"]

    pool: asyncpg.Pool | None = None
    alert = None

    if daemon:
        pool = await _get_pool(config)
        from alerts.logger_alert import LoggerAlert
        alert = LoggerAlert(config)

    while True:
        opps = await scan(config, prop_types, price_min, price_max, min_ev, hours_window)
        _print_opportunities(opps, f"{price_min:.0%}–{price_max:.0%}")

        if daemon and pool is not None:
            new_opps = await log_to_db(pool, opps, alert_min_roi, alert_min_depth)
            if new_opps and alert:
                await alert.prop_opportunity(new_opps)
            resolved = await resolve_outcomes(pool, gamma_url)
            if resolved:
                logger.info(f"Resolved outcomes for {resolved} prop markets")

        if not watch and not daemon:
            break
        if once:
            break

        interval = scan_interval if daemon else _REFRESH_INTERVAL
        print(f"  Next scan in {interval}s ... (Ctrl+C to exit)\n")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scan live Polymarket player prop markets for positive-EV opportunities"
    )
    p.add_argument(
        "--prop-types", nargs="+",
        default=_DEFAULT_PROP_TYPES,
        choices=["points", "rebounds", "assists"],
        help="Prop types to scan (default: all three)",
    )
    p.add_argument("--price-min", type=float, default=0.25,
                   help="Minimum YES price to consider (default: 0.25)")
    p.add_argument("--price-max", type=float, default=0.58,
                   help="Maximum YES price to consider (default: 0.58)")
    p.add_argument("--min-ev", type=float, default=0.03,
                   help="Minimum EV threshold as fraction of price (default: 3%% ROI)")
    p.add_argument("--hours", type=float, default=24.0,
                   help="Only show games starting within N hours (default: 24)")
    p.add_argument("--watch", action="store_true",
                   help=f"Auto-refresh every {_REFRESH_INTERVAL}s (no DB)")
    p.add_argument("--daemon", action="store_true",
                   help="Run as daemon: persist to DB, send alerts, resolve outcomes")
    p.add_argument("--once", action="store_true",
                   help="With --daemon: run exactly one cycle then exit (for testing)")
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

    try:
        asyncio.run(run(
            config,
            prop_types=args.prop_types,
            price_min=args.price_min,
            price_max=args.price_max,
            min_ev=args.min_ev,
            hours_window=args.hours,
            watch=args.watch,
            daemon=args.daemon,
            once=args.once,
        ))
    except KeyboardInterrupt:
        print("\nStopped.")
