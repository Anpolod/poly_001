"""
Historical Calibration Data Fetcher

Enumerates all resolved Polymarket sports markets via the Gamma API events feed,
fetches pre-game price history for each from the CLOB API, and stores everything
in the `historical_calibration` table for use by calibration_analyzer.

Two-phase design:
  Phase 1 — enumerate all resolved structured sports market IDs + outcomes
  Phase 2 — batch-fetch hourly CLOB price history for each market

Usage:
    python -m analytics.historical_fetcher
    python -m analytics.historical_fetcher --limit 500       # stop after N markets
    python -m analytics.historical_fetcher --sport moneyline # only moneyline type
    python -m analytics.historical_fetcher --skip-existing   # skip already-fetched IDs

The table is populated incrementally — safe to interrupt and re-run with --skip-existing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import asyncpg
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- concurrency knobs ---
_EVENT_CONCURRENCY = 20       # parallel event-page fetches
_CLOB_CONCURRENCY = 15        # parallel CLOB price-history fetches
_CLOB_DELAY = 0.05            # seconds between CLOB requests (per slot)
_EVENT_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class MarketMeta:
    market_id: str
    slug: str
    sport: str
    market_type: str
    game_start: datetime
    outcome: int               # 1 = YES won, 0 = NO won
    token_id: str              # YES-token CLOB id


@dataclass
class PriceOffsets:
    close: Optional[float] = None
    pre1h: Optional[float] = None
    pre2h: Optional[float] = None
    pre6h: Optional[float] = None
    pre12h: Optional[float] = None
    pre24h: Optional[float] = None
    pre48h: Optional[float] = None
    n_pts: int = 0


# ---------------------------------------------------------------------------
# Phase 1 — enumerate markets from Gamma events API
# ---------------------------------------------------------------------------


def _parse_sport(tags: list[dict]) -> str:
    """Derive sport label from event tags."""
    tag_labels = {t.get("label", "").lower() for t in tags if isinstance(t, dict)}
    if "basketball" in tag_labels or "nba" in tag_labels:
        return "basketball"
    if "football" in tag_labels or "soccer" in tag_labels:
        return "football"
    if "hockey" in tag_labels or "nhl" in tag_labels:
        return "hockey"
    if "baseball" in tag_labels or "mlb" in tag_labels:
        return "baseball"
    if "tennis" in tag_labels:
        return "tennis"
    return "other"


def _extract_markets(event: dict) -> list[MarketMeta]:
    """Parse markets from a single event dict."""
    markets: list[MarketMeta] = []
    mkts_raw = event.get("markets", [])
    mkts = json.loads(mkts_raw) if isinstance(mkts_raw, str) else mkts_raw

    tags = event.get("tags", [])
    sport = _parse_sport(tags)

    for m in mkts:
        if not isinstance(m, dict):
            continue
        if not m.get("sportsMarketType"):
            continue
        if not m.get("gameStartTime"):
            continue

        op_raw = m.get("outcomePrices", "[]")
        op = json.loads(op_raw) if isinstance(op_raw, str) else op_raw
        if not op or str(op[0]) not in ("0", "1", "0.0", "1.0"):
            continue  # unresolved or AMM-era fractional price

        token_raw = m.get("clobTokenIds", "[]")
        tokens = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        if not tokens:
            continue

        try:
            game_start = datetime.fromisoformat(
                m["gameStartTime"].replace(" ", "T").replace("+00", "+00:00")
            )
        except (ValueError, KeyError):
            continue

        markets.append(MarketMeta(
            market_id=str(m["id"]),
            slug=m.get("slug", ""),
            sport=sport,
            market_type=m.get("sportsMarketType", "unknown"),
            game_start=game_start,
            outcome=1 if float(op[0]) >= 0.5 else 0,
            token_id=str(tokens[0]),
        ))

    return markets


async def _fetch_event_page(
    session: aiohttp.ClientSession,
    gamma_url: str,
    offset: int,
    sem: asyncio.Semaphore,
) -> list[dict]:
    async with sem:
        try:
            async with session.get(
                f"{gamma_url}/events",
                params={
                    "tag": "sports",
                    "closed": "true",
                    "limit": _EVENT_PAGE_SIZE,
                    "order": "closedTime",
                    "ascending": "false",
                    "offset": offset,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    return []
                return await r.json()
        except Exception as exc:
            logger.warning(f"Event page offset={offset} failed: {exc}")
            return []


async def enumerate_and_process(
    gamma_url: str,
    clob_url: str,
    conn: asyncpg.Connection,
    market_type_filter: Optional[str],
    skip_ids: set[str],
    limit: int,
    write_batch_size: int = 500,
) -> int:
    """
    Stream through closed sports events page by page.
    Every write_batch_size markets: fetch CLOB prices and flush to DB immediately.
    Returns total markets inserted.
    """
    sem = asyncio.Semaphore(_EVENT_CONCURRENCY)
    total_inserted = 0
    total_seen = 0
    offset = 0
    pending: list[MarketMeta] = []

    async with aiohttp.ClientSession() as session:
        while True:
            # Fetch one page at a time to keep memory low
            page = await _fetch_event_page(session, gamma_url, offset, sem)
            if not page:
                break

            oldest_closed = page[-1].get("closedTime", "")[:7]
            stop = oldest_closed < "2025-10"

            for event in page:
                for m in _extract_markets(event):
                    if m.market_id in skip_ids:
                        continue
                    if market_type_filter and m.market_type != market_type_filter:
                        continue
                    pending.append(m)
                    total_seen += 1
                    if limit and total_seen >= limit:
                        stop = True
                        break
                if stop:
                    break

            # Flush when batch full or final page
            if len(pending) >= write_batch_size or stop or len(page) < _EVENT_PAGE_SIZE:
                if pending:
                    prices = await fetch_prices(clob_url, pending)
                    n = await save_to_db(conn, pending, prices)
                    total_inserted += n
                    skip_ids.update(m.market_id for m in pending)
                    logger.info(
                        f"  Flushed batch: +{n} rows | total={total_inserted} | "
                        f"offset={offset} | oldest_closed={oldest_closed}"
                    )
                    pending = []

            if stop or len(page) < _EVENT_PAGE_SIZE:
                break
            offset += _EVENT_PAGE_SIZE

    if pending:
        prices = await fetch_prices(clob_url, pending)
        n = await save_to_db(conn, pending, prices)
        total_inserted += n
        logger.info(f"  Final flush: +{n} rows | total={total_inserted}")

    return total_inserted


# ---------------------------------------------------------------------------
# Phase 2 — fetch CLOB price history
# ---------------------------------------------------------------------------


def _closest_price(
    history: list[dict],
    target_ts: float,
    tolerance_sec: int = 7200,  # 2h window
) -> Optional[float]:
    best = None
    best_diff = float("inf")
    for h in history:
        diff = abs(h["t"] - target_ts)
        if diff < best_diff and diff <= tolerance_sec:
            best_diff = diff
            best = h["p"]
    return best


def _price_before(history: list[dict], cutoff_ts: float) -> Optional[float]:
    """Last price strictly before cutoff_ts."""
    best = None
    for h in history:
        if h["t"] < cutoff_ts:
            best = h["p"]
    return best


async def _fetch_price_offsets(
    session: aiohttp.ClientSession,
    clob_url: str,
    token_id: str,
    game_start: datetime,
    sem: asyncio.Semaphore,
) -> PriceOffsets:
    game_ts = game_start.timestamp()
    # Fetch from 3 days before to 1h after game start (cover most creation windows)
    start_ts = int(game_ts - 86400 * 3)
    end_ts = int(game_ts + 3600)

    async with sem:
        try:
            async with session.get(
                f"{clob_url}/prices-history",
                params={
                    "market": token_id,
                    "startTs": start_ts,
                    "endTs": end_ts,
                    "fidelity": 60,   # 1 hour candles
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    return PriceOffsets()
                data = await r.json()
        except Exception as exc:
            logger.debug(f"CLOB fetch failed for {token_id[:20]}: {exc}")
            return PriceOffsets()
        finally:
            await asyncio.sleep(_CLOB_DELAY)

    history = data.get("history", [])
    if not history:
        return PriceOffsets()

    return PriceOffsets(
        close=_price_before(history, game_ts),
        pre1h=_closest_price(history, game_ts - 3600),
        pre2h=_closest_price(history, game_ts - 7200),
        pre6h=_closest_price(history, game_ts - 21600),
        pre12h=_closest_price(history, game_ts - 43200),
        pre24h=_closest_price(history, game_ts - 86400),
        pre48h=_closest_price(history, game_ts - 172800),
        n_pts=len(history),
    )


async def fetch_prices(
    clob_url: str,
    markets: list[MarketMeta],
) -> dict[str, PriceOffsets]:
    sem = asyncio.Semaphore(_CLOB_CONCURRENCY)
    results: dict[str, PriceOffsets] = {}

    async def _one(session: aiohttp.ClientSession, m: MarketMeta) -> None:
        offsets = await _fetch_price_offsets(session, clob_url, m.token_id, m.game_start, sem)
        results[m.market_id] = offsets

    async with aiohttp.ClientSession() as session:
        tasks = [asyncio.create_task(_one(session, m)) for m in markets]
        done = 0
        for coro in asyncio.as_completed(tasks):
            await coro
            done += 1
            if done % 500 == 0:
                with_price = sum(1 for p in results.values() if p.close is not None)
                logger.info(f"  CLOB: {done}/{len(markets)} fetched, {with_price} with price data ...")

    with_price = sum(1 for p in results.values() if p.close is not None)
    logger.info(f"Phase 2 complete: {with_price}/{len(markets)} markets have price data")
    return results


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------


async def save_to_db(
    conn: asyncpg.Connection,
    markets: list[MarketMeta],
    prices: dict[str, PriceOffsets],
) -> int:
    inserted = 0
    for m in markets:
        p = prices.get(m.market_id, PriceOffsets())
        try:
            await conn.execute(
                """
                INSERT INTO historical_calibration (
                    market_id, slug, sport, market_type, game_start, outcome,
                    price_close, price_pre1h, price_pre2h, price_pre6h,
                    price_pre12h, price_pre24h, price_pre48h, n_price_pts
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                ON CONFLICT (market_id) DO UPDATE SET
                    price_close  = EXCLUDED.price_close,
                    price_pre1h  = EXCLUDED.price_pre1h,
                    price_pre2h  = EXCLUDED.price_pre2h,
                    price_pre6h  = EXCLUDED.price_pre6h,
                    price_pre12h = EXCLUDED.price_pre12h,
                    price_pre24h = EXCLUDED.price_pre24h,
                    price_pre48h = EXCLUDED.price_pre48h,
                    n_price_pts  = EXCLUDED.n_price_pts,
                    fetched_at   = NOW()
                """,
                m.market_id, m.slug, m.sport, m.market_type,
                m.game_start, m.outcome,
                p.close, p.pre1h, p.pre2h, p.pre6h,
                p.pre12h, p.pre24h, p.pre48h, p.n_pts,
            )
            inserted += 1
        except Exception as exc:
            logger.warning(f"DB insert failed for {m.market_id}: {exc}")
    return inserted


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run(
    config: dict,
    market_type_filter: Optional[str] = None,
    limit: int = 0,
    skip_existing: bool = True,
) -> None:
    db = config["database"]
    gamma_url = config["api"]["gamma_base_url"]
    clob_url = config["api"]["clob_base_url"]

    conn = await asyncpg.connect(
        host=db["host"], port=db["port"],
        database=db["name"], user=db["user"], password=db["password"],
    )

    try:
        skip_ids: set[str] = set()
        if skip_existing:
            rows = await conn.fetch(
                "SELECT market_id FROM historical_calibration WHERE price_close IS NOT NULL"
            )
            skip_ids = {r["market_id"] for r in rows}
            logger.info(f"Skipping {len(skip_ids)} already-fetched markets")

        logger.info("Streaming events → CLOB prices → DB ...")
        total = await enumerate_and_process(
            gamma_url, clob_url, conn,
            market_type_filter, skip_ids, limit,
        )

        rows = await conn.fetch(
            """
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN price_close IS NOT NULL THEN 1 ELSE 0 END) as with_price
            FROM historical_calibration
            """
        )
        r = rows[0]
        logger.info(
            f"Done. This run: {total} rows. "
            f"Table total: {r['total']} markets, {r['with_price']} with price data."
        )

    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch historical sports market outcomes + pre-game prices into historical_calibration"
    )
    p.add_argument("--limit", type=int, default=0, help="Stop after N markets (0 = all)")
    p.add_argument(
        "--market-type", default=None,
        choices=["moneyline", "spreads", "totals"],
        help="Filter by market type",
    )
    p.add_argument(
        "--no-skip", action="store_true",
        help="Re-fetch markets already in the table (default: skip existing)",
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

    asyncio.run(run(
        config,
        market_type_filter=args.market_type,
        limit=args.limit,
        skip_existing=not args.no_skip,
    ))
