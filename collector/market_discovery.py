"""Пошук і фільтрація ринків для збору даних"""

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import yaml

from .rest_client import RestClient

logger = logging.getLogger(__name__)

SPORTS_LEAGUES = {
    "nba", "nhl", "mlb", "nfl", "ncaa", "ncaa basketball",
    "atp", "wta",
    "premier league", "epl", "la liga", "serie a", "bundesliga",
    "ligue 1", "champions league", "europa league",
    "mls",
}

SPORTS_KEYWORDS = {
    "basketball", "hockey", "baseball", "football", "soccer",
    "tennis", "cricket",
}

NON_SPORTS_PATTERNS = [
    "elon-musk", "trump", "tweet", "ceasefire", "war",
    "election", "president", "bitcoin", "ethereum", "crypto",
    "chess", "weather", "temperature",
]

# Sport whitelist for filter_tradeable
TRADEABLE_SPORTS = {"basketball", "football", "tennis", "baseball", "hockey"}

# Slug prefix → sport mapping
_SLUG_PREFIX_MAP = [
    ("nba-", "basketball"),
    ("nhl-", "hockey"),
    ("mlb-", "baseball"),
    ("nfl-", "football"),
    ("atp-", "tennis"),
    ("wta-", "tennis"),
    ("epl-", "football"),
    ("lal-", "football"),
    ("bun-", "football"),
    ("fl1-", "football"),
    ("ser-", "football"),
    ("ucl-", "football"),
    ("elc-", "football"),
    ("efa-", "football"),
    ("cbb-", "basketball"),
    ("por-", "football"),
    ("fr2-", "football"),
]


@dataclass
class MarketInfo:
    market_id: str
    slug: str
    sport: str
    league: str
    event_start: datetime
    token_id_yes: str
    token_id_no: str
    best_bid: float
    best_ask: float
    mid_price: float
    spread: float
    spread_pct: float
    bid_depth: float
    ask_depth: float
    volume_24h: float
    time_to_event_h: float
    verdict: str  # "TRADEABLE" or "FILTERED: <reason>"


def detect_sport(slug: str) -> str:
    """Detect sport from slug prefix. Returns 'unknown' if no match."""
    slug_lower = slug.lower()
    for prefix, sport in _SLUG_PREFIX_MAP:
        if slug_lower.startswith(prefix):
            return sport
    return "unknown"


class MarketDiscovery:
    def __init__(self, rest_client: RestClient, config: dict):
        self.rest = rest_client
        self.config = config

    async def discover_all_sports_markets(self) -> list[dict]:
        """Знайти всі активні спортивні ринки"""
        events = await self.rest.get_sports_events()
        markets = []
        for event in events:
            parsed = self.rest.parse_event(event)
            markets.extend(parsed)

        # Фільтр: тільки майбутні events
        now = datetime.now(timezone.utc)
        markets = [m for m in markets if m["event_start"] > now]

        logger.info(
            f"Всього спортивних ринків з майбутнім event_start: {len(markets)}"
        )
        return markets

    def is_sports_market(self, market: dict) -> bool:
        """Перевірити чи це справді спортивний ринок"""
        slug = (market.get("slug") or "").lower()

        # Blacklist
        for pattern in NON_SPORTS_PATTERNS:
            if pattern in slug:
                return False

        # Whitelist по лізі
        league = (market.get("league") or "").lower()
        if league in SPORTS_LEAGUES:
            return True

        # Whitelist по спорту
        sport = (market.get("sport") or "").lower()
        if sport in SPORTS_KEYWORDS:
            return True

        # Slug-based detection
        sports_slug_prefixes = [
            "nba-", "nhl-", "mlb-", "nfl-", "atp-", "wta-",
            "epl-", "lal-", "bun-", "fl1-", "ser-", "ucl-",
            "elc-", "efa-",
        ]
        for prefix in sports_slug_prefixes:
            if slug.startswith(prefix):
                return True

        return False

    def filter_for_phase0(self, markets: list[dict]) -> list[dict]:
        """Фільтр для Фази 0: мінімальний volume + sports whitelist"""
        min_vol = self.config["phase0"]["min_volume_24h"]
        filtered = [
            m for m in markets
            if m.get("volume_24h", 0) >= min_vol and self.is_sports_market(m)
        ]
        logger.info(
            f"Після фільтру volume ≥ ${min_vol} + sports: {len(filtered)} ринків"
        )
        return filtered

    def filter_for_phase1(self, markets: list[dict], orderbooks: dict) -> list[dict]:
        """Фільтр для Фази 1: volume + spread + depth"""
        cfg = self.config["phase1"]
        min_vol = cfg["min_volume_24h"]
        max_spread = cfg["max_spread"]
        min_depth = cfg["min_depth"]

        filtered = []
        for m in markets:
            if m.get("volume_24h", 0) < min_vol:
                continue

            ob = orderbooks.get(m["id"])
            if not ob:
                continue
            if ob["spread"] is None or ob["spread"] > max_spread:
                continue
            if ob["bid_depth"] < min_depth or ob["ask_depth"] < min_depth:
                continue

            filtered.append(m)

        logger.info(
            f"Після фільтру Phase1 (vol≥${min_vol}, spread≤{max_spread}, "
            f"depth≥${min_depth}): {len(filtered)} ринків"
        )
        return filtered

    def group_by_league(self, markets: list[dict]) -> dict[str, list[dict]]:
        """Групувати ринки по лізі"""
        groups = {}
        for m in markets:
            key = f"{m['sport']}/{m['league']}"
            groups.setdefault(key, []).append(m)
        return groups

    def compute_liquidity_metrics(self, orderbook: dict) -> dict:
        """Compute liquidity metrics from an orderbook dict.

        Args:
            orderbook: Raw orderbook dict as returned by RestClient.get_orderbook().
                       Expected keys: best_bid, best_ask, bid_depth, ask_depth.

        Returns:
            Dict with keys: best_bid, best_ask, spread, spread_pct, bid_depth_usd,
            ask_depth_usd, mid_price.
        """
        best_bid = float(orderbook.get("best_bid", 0))
        best_ask = float(orderbook.get("best_ask", 0))
        bid_depth = float(orderbook.get("bid_depth", 0))
        ask_depth = float(orderbook.get("ask_depth", 0))

        spread = best_ask - best_bid
        mid_price = (best_bid + best_ask) / 2 if (best_bid + best_ask) > 0 else 0.0
        spread_pct = (spread / mid_price) if mid_price > 0 else 0.0

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_pct": spread_pct,
            "bid_depth_usd": bid_depth,
            "ask_depth_usd": ask_depth,
            "mid_price": mid_price,
        }

    def filter_tradeable(
        self, markets: list[dict], orderbooks: dict
    ) -> list[MarketInfo]:
        """Apply liquidity and quality filters to produce a list of MarketInfo.

        Filters applied in order (each skip logged at DEBUG):
          1. min_volume_24h >= 10 000
          2. max_spread <= 0.03
          3. min_depth >= 1 000 (min of bid/ask depth)
          4. exclude extreme odds: mid < 0.15 or mid > 0.85
          5. sport whitelist: basketball, football, tennis, baseball, hockey
          6. exclude strong favorites: mid > 0.80

        Args:
            markets: List of market dicts (as returned by parse_event).
            orderbooks: Dict mapping market_id → orderbook dict.

        Returns:
            List of MarketInfo dataclasses with verdict="TRADEABLE".
        """
        ft = self.config.get("filter_tradeable", {})
        MIN_VOLUME      = float(ft.get("min_volume_24h",   self.config["phase1"]["min_volume_24h"]))
        MAX_SPREAD      = float(ft.get("max_spread",       self.config["phase1"]["max_spread"]))
        MIN_DEPTH       = float(ft.get("min_depth",        self.config["phase1"]["min_depth"]))
        MID_EXTREME_LOW = float(ft.get("mid_extreme_low",  0.15))
        MID_EXTREME_HIGH= float(ft.get("mid_extreme_high", 0.85))
        MID_FAVORITE    = float(ft.get("mid_favorite",     0.80))

        now = datetime.now(timezone.utc)
        result: list[MarketInfo] = []

        logger.info(f"filter_tradeable: starting with {len(markets)} markets")

        for m in markets:
            market_id = m.get("id", "")
            slug = m.get("slug", "")
            volume_24h = float(m.get("volume_24h", 0))

            # --- Filter 1: minimum 24h volume ---
            if volume_24h < MIN_VOLUME:
                logger.debug(
                    f"FILTERED [{slug}]: volume_24h={volume_24h:.0f} < {MIN_VOLUME:.0f}"
                )
                continue

            # --- Resolve orderbook ---
            ob = orderbooks.get(market_id)
            if not ob:
                logger.debug(f"FILTERED [{slug}]: no orderbook available")
                continue

            metrics = self.compute_liquidity_metrics(ob)
            best_bid = metrics["best_bid"]
            best_ask = metrics["best_ask"]
            spread = metrics["spread"]
            spread_pct = metrics["spread_pct"]
            mid_price = metrics["mid_price"]
            bid_depth = metrics["bid_depth_usd"]
            ask_depth = metrics["ask_depth_usd"]

            # --- Filter 2: max spread ---
            if spread > MAX_SPREAD:
                logger.debug(
                    f"FILTERED [{slug}]: spread={spread:.4f} > {MAX_SPREAD}"
                )
                continue

            # --- Filter 3: minimum depth ---
            min_depth_val = min(bid_depth, ask_depth)
            if min_depth_val < MIN_DEPTH:
                logger.debug(
                    f"FILTERED [{slug}]: min_depth={min_depth_val:.0f} < {MIN_DEPTH:.0f}"
                )
                continue

            # --- Filter 4: exclude extreme odds ---
            if mid_price < MID_EXTREME_LOW or mid_price > MID_EXTREME_HIGH:
                logger.debug(
                    f"FILTERED [{slug}]: extreme mid={mid_price:.3f} "
                    f"(outside [{MID_EXTREME_LOW}, {MID_EXTREME_HIGH}])"
                )
                continue

            # --- Filter 5: sport whitelist ---
            sport_raw = (m.get("sport") or "").lower()
            sport = sport_raw if sport_raw in TRADEABLE_SPORTS else detect_sport(slug)
            if sport not in TRADEABLE_SPORTS:
                logger.debug(
                    f"FILTERED [{slug}]: sport='{sport}' not in whitelist"
                )
                continue

            # --- Filter 6: exclude strong favorites ---
            if mid_price > MID_FAVORITE:
                logger.debug(
                    f"FILTERED [{slug}]: strong favorite mid={mid_price:.3f} > {MID_FAVORITE}"
                )
                continue

            # --- All filters passed ---
            event_start: datetime = m["event_start"]
            tte_h = (event_start - now).total_seconds() / 3600.0

            info = MarketInfo(
                market_id=market_id,
                slug=slug,
                sport=sport,
                league=(m.get("league") or "unknown"),
                event_start=event_start,
                token_id_yes=(m.get("token_id_yes") or ""),
                token_id_no=(m.get("token_id_no") or ""),
                best_bid=best_bid,
                best_ask=best_ask,
                mid_price=mid_price,
                spread=spread,
                spread_pct=spread_pct,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                volume_24h=volume_24h,
                time_to_event_h=tte_h,
                verdict="TRADEABLE",
            )
            result.append(info)

        logger.info(
            f"filter_tradeable: {len(result)} TRADEABLE markets "
            f"from {len(markets)} candidates"
        )
        return result


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

async def _run_dry_run(config_path: str) -> None:
    with open(config_path) as fh:
        config = yaml.safe_load(fh)

    rest = RestClient(config)
    await rest.start()
    try:
        discovery = MarketDiscovery(rest, config)

        logger.info("Discovering sports markets...")
        markets = await discovery.discover_all_sports_markets()
        logger.info(f"Total markets discovered: {len(markets)}")

        # Fetch orderbooks for all markets
        logger.info("Fetching orderbooks...")
        orderbooks: dict = {}
        for m in markets:
            mid = m.get("id", "")
            token_yes = m.get("token_id_yes")
            if not token_yes:
                continue
            ob = await rest.get_orderbook(token_yes)
            if ob:
                orderbooks[mid] = ob

        logger.info(f"Orderbooks fetched: {len(orderbooks)}")

        tradeable = discovery.filter_tradeable(markets, orderbooks)

        # Print table
        headers = ["slug", "sport", "mid", "spread", "depth_min", "vol_24h", "tte_h", "verdict"]
        rows = []
        for mi in tradeable:
            rows.append([
                mi.slug[:40],
                mi.sport,
                f"{mi.mid_price:.3f}",
                f"{mi.spread:.4f}",
                f"{min(mi.bid_depth, mi.ask_depth):.0f}",
                f"{mi.volume_24h:.0f}",
                f"{mi.time_to_event_h:.1f}",
                mi.verdict,
            ])

        try:
            from tabulate import tabulate  # type: ignore
            print(tabulate(rows, headers=headers, tablefmt="simple"))
        except ImportError:
            # Plain fallback
            col_widths = [max(len(str(r[i])) for r in ([headers] + rows)) for i in range(len(headers))]
            fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
            print(fmt.format(*headers))
            print("  ".join("-" * w for w in col_widths))
            for row in rows:
                print(fmt.format(*row))

        print(f"\nTotal TRADEABLE markets: {len(tradeable)}")
    finally:
        await rest.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Polymarket sports market discovery CLI"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover, filter, and print tradeable markets without trading",
    )
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="Path to settings YAML (default: config/settings.yaml)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logging (shows filter skip reasons)",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.dry_run:
        asyncio.run(_run_dry_run(args.config))
    else:
        parser.print_help()
