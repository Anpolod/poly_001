"""
Pre-game Pricing Calibration Analyzer

Fetches actual resolution outcomes from the Polymarket API for all settled markets
and compares them against pre-game snapshot prices to identify systematic mispricings.

Analysis produced:
  1. Calibration curve — are 60% markets winning 60% of the time?
  2. Favorite-longshot bias — do heavy favorites / longshots get mispriced?
  3. Sport breakdown — which sports show the largest calibration error?
  4. Market type breakdown — moneyline vs. spread vs. total
  5. Price drift — how much do prices move in the final 1h / 2h / 6h before game start?

Usage:
    python -m analytics.calibration_analyzer
    python -m analytics.calibration_analyzer --sport hockey
    python -m analytics.calibration_analyzer --window 2   # hours before start
    python -m analytics.calibration_analyzer --output calibration.csv
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiohttp
import asyncpg
import pandas as pd
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_API_CONCURRENCY = 10          # parallel API requests
_REQUEST_DELAY = 0.15          # seconds between batches


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class MarketRecord:
    """One settled market with pre-game price and actual outcome."""
    market_id: str
    slug: str
    sport: str
    market_type: str            # moneyline | spread | total | unknown
    event_start: datetime
    outcome: int                # 1 = YES won, 0 = NO won
    price_opening: Optional[float]    # first snapshot ever captured
    price_pre1h: Optional[float]      # closest snapshot ≥ 1h before event_start
    price_pre2h: Optional[float]
    price_pre6h: Optional[float]
    price_pre12h: Optional[float]
    price_close: Optional[float]      # last snapshot before event_start


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


async def _fetch_resolution(
    session: aiohttp.ClientSession,
    base_url: str,
    market_id: str,
) -> Optional[dict]:
    """Fetch market metadata + resolution from Gamma API."""
    try:
        async with session.get(f"{base_url}/markets/{market_id}", timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            # outcomePrices is a JSON-encoded list like '["1", "0"]' or '["0", "1"]'
            import json as _json
            op_raw = data.get("outcomePrices")
            if not op_raw:
                return None
            op = _json.loads(op_raw)
            if not op or op[0] not in ("0", "1", "0.0", "1.0"):
                return None
            return {
                "outcome": 1 if float(op[0]) >= 0.5 else 0,
                "market_type": data.get("sportsMarketType") or "unknown",
                "game_start": data.get("gameStartTime"),
            }
    except Exception as exc:
        logger.debug(f"API fetch failed for {market_id}: {exc}")
        return None


async def _fetch_all_resolutions(
    market_ids: list[str],
    base_url: str,
) -> dict[str, dict]:
    """Concurrently fetch resolutions for all markets, respecting rate limits."""
    results: dict[str, dict] = {}
    sem = asyncio.Semaphore(_API_CONCURRENCY)

    async def _one(session: aiohttp.ClientSession, mid: str) -> None:
        async with sem:
            data = await _fetch_resolution(session, base_url, mid)
            if data:
                results[mid] = data
            await asyncio.sleep(_REQUEST_DELAY)

    async with aiohttp.ClientSession() as session:
        tasks = [asyncio.create_task(_one(session, mid)) for mid in market_ids]
        for i, task in enumerate(asyncio.as_completed(tasks), 1):
            await task
            if i % 20 == 0:
                logger.info(f"  Fetched {i}/{len(market_ids)} market resolutions ...")

    logger.info(f"  Resolved {len(results)}/{len(market_ids)} markets from API")
    return results


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _load_settled_markets(
    conn: asyncpg.Connection,
    sport_filter: Optional[str],
) -> list[dict]:
    q = """
        SELECT id, slug, sport, event_start
        FROM markets
        WHERE status = 'settled'
          AND event_start IS NOT NULL
    """
    params: list = []
    if sport_filter:
        q += " AND sport = $1"
        params.append(sport_filter)
    rows = await conn.fetch(q, *params)
    return [dict(r) for r in rows]


async def _price_at_offset(
    conn: asyncpg.Connection,
    market_id: str,
    before_ts: datetime,
    window_hours: float,
) -> Optional[float]:
    """Return mid_price closest to (before_ts - window_hours), within a ±30min tolerance."""
    target = before_ts - timedelta(hours=window_hours)
    row = await conn.fetchrow(
        """
        SELECT mid_price::float
        FROM price_snapshots
        WHERE market_id = $1
          AND ts BETWEEN $2 AND $3
        ORDER BY ABS(EXTRACT(EPOCH FROM (ts - $4)))
        LIMIT 1
        """,
        market_id,
        target - timedelta(minutes=30),
        target + timedelta(minutes=30),
        target,
    )
    return float(row["mid_price"]) if row else None


async def _load_price_snapshots_for_market(
    conn: asyncpg.Connection,
    market_id: str,
    event_start: datetime,
) -> dict:
    """Return price_opening, price_close, and offset prices for one market."""
    # Opening price
    first = await conn.fetchrow(
        "SELECT mid_price::float FROM price_snapshots WHERE market_id = $1 ORDER BY ts LIMIT 1",
        market_id,
    )
    # Last snapshot BEFORE event_start (pre-game close)
    close = await conn.fetchrow(
        """
        SELECT mid_price::float FROM price_snapshots
        WHERE market_id = $1 AND ts < $2
        ORDER BY ts DESC LIMIT 1
        """,
        market_id, event_start,
    )

    return {
        "price_opening": float(first["mid_price"]) if first else None,
        "price_close": float(close["mid_price"]) if close else None,
        "price_pre1h": await _price_at_offset(conn, market_id, event_start, 1.0),
        "price_pre2h": await _price_at_offset(conn, market_id, event_start, 2.0),
        "price_pre6h": await _price_at_offset(conn, market_id, event_start, 6.0),
        "price_pre12h": await _price_at_offset(conn, market_id, event_start, 12.0),
    }


# ---------------------------------------------------------------------------
# Calibration metrics
# ---------------------------------------------------------------------------


def _calibration_table(df: pd.DataFrame, price_col: str, n_buckets: int = 5) -> pd.DataFrame:
    """
    Group markets by price bucket and compute actual win rate per bucket.
    Returns a DataFrame with columns: bucket_mid, n_markets, actual_win_rate, expected_win_rate, error.
    """
    sub = df[df[price_col].notna()].copy()
    if sub.empty:
        return pd.DataFrame()

    sub["bucket"] = pd.cut(sub[price_col], bins=n_buckets, labels=False)
    grp = sub.groupby("bucket").agg(
        n=("outcome", "count"),
        actual_win_rate=("outcome", "mean"),
        avg_price=(price_col, "mean"),
    ).reset_index()
    grp["expected_win_rate"] = grp["avg_price"]
    grp["error_pct"] = (grp["actual_win_rate"] - grp["expected_win_rate"]) * 100
    return grp[grp["n"] >= 3]


def _favorite_longshot_bias(df: pd.DataFrame, price_col: str) -> pd.DataFrame:
    """
    Split into favorites (>0.6), neutral (0.4–0.6), longshots (<0.4).
    Check whether each group is over- or under-priced vs. actual win rate.
    """
    sub = df[df[price_col].notna()].copy()
    if sub.empty:
        return pd.DataFrame()

    def _tier(p: float) -> str:
        if p >= 0.70:
            return "heavy fav (≥0.70)"
        elif p >= 0.60:
            return "fav (0.60-0.70)"
        elif p >= 0.40:
            return "neutral (0.40-0.60)"
        elif p >= 0.30:
            return "dog (0.30-0.40)"
        else:
            return "heavy dog (<0.30)"

    sub["tier"] = sub[price_col].apply(_tier)
    grp = sub.groupby("tier").agg(
        n=("outcome", "count"),
        actual_win_rate=("outcome", "mean"),
        avg_price=(price_col, "mean"),
    ).reset_index()
    grp["expected_win_rate"] = grp["avg_price"]
    grp["edge_pct"] = (grp["actual_win_rate"] - grp["expected_win_rate"]) * 100
    return grp[grp["n"] >= 3].sort_values("avg_price", ascending=False)


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------


def _print_section(title: str, df: pd.DataFrame) -> None:
    if df.empty:
        print(f"  (no data for {title})\n")
        return
    print(f"\n  {title}")
    print("  " + "-" * 80)
    print("  " + df.to_string(index=False).replace("\n", "\n  "))
    print()


def _print_report(records: list[MarketRecord], price_window: str) -> None:
    df = pd.DataFrame([r.__dict__ for r in records])

    price_col = {
        "opening": "price_opening",
        "close": "price_close",
        "1h": "price_pre1h",
        "2h": "price_pre2h",
        "6h": "price_pre6h",
        "12h": "price_pre12h",
    }.get(price_window, "price_close")

    with_price = df[price_col].notna().sum()
    resolved = df["outcome"].notna().sum()

    print(f"\n{'='*90}")
    print("  PRE-GAME CALIBRATION ANALYSIS")
    print(
        f"  Price window: {price_window} before event_start  |  "
        f"{resolved} markets with outcomes  |  {with_price} with price data"
    )
    print(f"{'='*90}")

    # --- Overall calibration ---
    print(f"\n{'─'*90}")
    print("  OVERALL CALIBRATION (price bucket → actual win rate)")
    cal = _calibration_table(df, price_col)
    if not cal.empty:
        cal_display = cal[["avg_price", "n", "actual_win_rate", "expected_win_rate", "error_pct"]].copy()
        cal_display.columns = ["avg_price", "n_markets", "actual_win%", "expected_win%", "error_pct"]
        _print_section("Calibration buckets", cal_display.round(3))

    # --- Favorite-longshot bias ---
    print(f"{'─'*90}")
    print("  FAVORITE-LONGSHOT BIAS")
    flb = _favorite_longshot_bias(df, price_col)
    if not flb.empty:
        flb_display = flb[["tier", "n", "avg_price", "actual_win_rate", "expected_win_rate", "edge_pct"]].copy()
        flb_display.columns = ["tier", "n", "avg_price", "actual_win%", "expected_win%", "edge_pct%"]
        _print_section("Favorite-longshot bias", flb_display.round(3))

    # --- By sport ---
    print(f"{'─'*90}")
    print("  CALIBRATION ERROR BY SPORT")
    sub = df[df[price_col].notna()].copy()
    if not sub.empty:
        sport_grp = sub.groupby("sport").agg(
            n=("outcome", "count"),
            actual_win_rate=("outcome", "mean"),
            avg_price=(price_col, "mean"),
        ).reset_index()
        sport_grp["expected_win_rate"] = sport_grp["avg_price"]
        sport_grp["error_pct"] = (sport_grp["actual_win_rate"] - sport_grp["expected_win_rate"]) * 100
        sport_grp = sport_grp[sport_grp["n"] >= 3].sort_values("error_pct")
        _print_section("By sport", sport_grp.round(3))

    # --- By market type ---
    print(f"{'─'*90}")
    print("  CALIBRATION ERROR BY MARKET TYPE")
    if not sub.empty:
        mt_grp = sub.groupby("market_type").agg(
            n=("outcome", "count"),
            actual_win_rate=("outcome", "mean"),
            avg_price=(price_col, "mean"),
        ).reset_index()
        mt_grp["expected_win_rate"] = mt_grp["avg_price"]
        mt_grp["error_pct"] = (mt_grp["actual_win_rate"] - mt_grp["expected_win_rate"]) * 100
        mt_grp = mt_grp[mt_grp["n"] >= 3].sort_values("error_pct")
        _print_section("By market type", mt_grp.round(3))

    # --- Price drift analysis (pre24h → close) ---
    print(f"{'─'*90}")
    print("  PRICE DRIFT (24h-before price → pre-game close)")
    anchor_col = "price_pre24h" if "price_pre24h" in df.columns else "price_opening"
    drift_df = df[df[anchor_col].notna() & df["price_close"].notna()].copy()
    if not drift_df.empty:
        drift_df["drift"] = drift_df["price_close"] - drift_df[anchor_col]
        drift_df["abs_drift"] = drift_df["drift"].abs()
        drift_df["drifted_toward_winner"] = (
            (drift_df["outcome"] == 1) & (drift_df["drift"] > 0) |
            (drift_df["outcome"] == 0) & (drift_df["drift"] < 0)
        )
        drift_summary = drift_df.groupby("sport").agg(
            n=("drift", "count"),
            avg_abs_drift=("abs_drift", "mean"),
            pct_toward_winner=("drifted_toward_winner", "mean"),
        ).reset_index()
        drift_summary["pct_toward_winner"] *= 100
        _print_section("Price drift by sport", drift_summary.round(4))

        overall_drift = drift_df["drifted_toward_winner"].mean() * 100
        print(f"  Overall: price drifted toward winner in {overall_drift:.1f}% of markets")

    print(f"\n{'='*90}\n")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _load_from_historical_calibration(
    conn: asyncpg.Connection,
    sport_filter: Optional[str],
    market_type_filter: Optional[str],
) -> list[MarketRecord]:
    """Load pre-fetched historical records directly from the historical_calibration table."""
    q = """
        SELECT market_id, slug, sport, market_type, game_start,
               outcome,
               price_close::float,
               price_pre1h::float,
               price_pre2h::float,
               price_pre6h::float,
               price_pre12h::float,
               price_pre24h::float,
               price_pre48h::float
        FROM historical_calibration
        WHERE outcome IS NOT NULL
    """
    params: list = []
    if sport_filter:
        q += f" AND sport = ${len(params)+1}"
        params.append(sport_filter)
    if market_type_filter:
        q += f" AND market_type = ${len(params)+1}"
        params.append(market_type_filter)

    rows = await conn.fetch(q, *params)
    records = []
    for r in rows:
        records.append(MarketRecord(
            market_id=r["market_id"],
            slug=r["slug"] or "",
            sport=r["sport"] or "other",
            market_type=r["market_type"] or "unknown",
            event_start=r["game_start"],
            outcome=r["outcome"],
            price_opening=None,       # not stored in historical_calibration
            price_pre1h=r["price_pre1h"],
            price_pre2h=r["price_pre2h"],
            price_pre6h=r["price_pre6h"],
            price_pre12h=r["price_pre12h"],
            price_close=r["price_close"],
        ))
    return records


async def run(
    config: dict,
    sport_filter: Optional[str] = None,
    market_type_filter: Optional[str] = None,
    price_window: str = "close",
    use_historical: bool = True,
    output: Optional[str] = None,
) -> list[MarketRecord]:
    db = config["database"]
    base_url = config["api"]["gamma_base_url"]

    conn = await asyncpg.connect(
        host=db["host"], port=db["port"],
        database=db["name"], user=db["user"], password=db["password"],
    )

    try:
        # Check if historical_calibration table has data
        if use_historical:
            count = await conn.fetchval("SELECT COUNT(*) FROM historical_calibration WHERE price_close IS NOT NULL")
            if count and count > 100:
                logger.info(f"Loading {count:,} records from historical_calibration table ...")
                records = await _load_from_historical_calibration(conn, sport_filter, market_type_filter)
                logger.info(f"Loaded {len(records)} records")
                _print_report(records, price_window)
                if output:
                    pd.DataFrame([r.__dict__ for r in records]).to_csv(output, index=False)
                    logger.info(f"Saved to {output}")
                return records

        markets = await _load_settled_markets(conn, sport_filter)
        logger.info(f"Found {len(markets)} settled markets{' (sport=' + sport_filter + ')' if sport_filter else ''}")

        # Fetch resolutions from API
        logger.info("Fetching resolutions from Polymarket API ...")
        market_ids = [m["id"] for m in markets]
        resolutions = await _fetch_all_resolutions(market_ids, base_url)

        # Build records
        records = []
        for m in markets:
            res = resolutions.get(m["id"])
            if res is None:
                continue  # no resolution data — skip

            prices = await _load_price_snapshots_for_market(conn, m["id"], m["event_start"])

            records.append(MarketRecord(
                market_id=m["id"],
                slug=m["slug"],
                sport=m["sport"],
                market_type=res.get("market_type", "unknown"),
                event_start=m["event_start"],
                outcome=res["outcome"],
                **prices,
            ))

    finally:
        await conn.close()

    logger.info(f"Built {len(records)} records with resolution + snapshot data")

    _print_report(records, price_window)

    if output:
        pd.DataFrame([r.__dict__ for r in records]).to_csv(output, index=False)
        logger.info(f"Saved to {output}")

    return records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pre-game calibration analysis for settled markets")
    p.add_argument("--sport", default=None, help="Filter by sport (hockey, basketball, baseball, football, tennis)")
    p.add_argument("--market-type", default=None, help="Filter by market type (moneyline, spreads, totals)")
    p.add_argument(
        "--window", default="close",
        choices=["close", "1h", "2h", "6h", "12h"],
        help="Which pre-game price to use for calibration (default: close = last before event_start)",
    )
    p.add_argument("--no-historical", action="store_true",
                   help="Force use of live price_snapshots + API instead of historical_calibration table")
    p.add_argument("--output", default=None, help="Save full record table to CSV")
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

    asyncio.run(run(
        config,
        sport_filter=args.sport,
        market_type_filter=args.market_type,
        price_window=args.window,
        use_historical=not args.no_historical,
        output=args.output,
    ))
