"""REST client for the Polymarket CLOB and Gamma APIs."""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional

import aiohttp

from collector.network import make_connector

logger = logging.getLogger(__name__)


class RestClient:
    """Async HTTP client for the Polymarket Gamma API (events/markets) and CLOB API (orderbooks, fees, history)."""

    def __init__(self, config: dict):
        self.gamma_url = config["api"]["gamma_base_url"]
        self.clob_url = config["api"]["clob_base_url"]
        self.delay = config["api"]["request_delay_sec"]
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        """Open the underlying aiohttp session. Must be called before any requests."""
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(connector=make_connector(), timeout=timeout)

    async def close(self):
        """Close the aiohttp session and release connections."""
        if self.session:
            await self.session.close()

    async def _get(self, url: str, params: dict = None) -> Optional[Any]:
        """GET request with retry and rate limiting."""
        for attempt in range(3):
            try:
                await asyncio.sleep(self.delay)
                async with self.session.get(url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        wait = 30 * (attempt + 1)
                        logger.warning(f"Rate limited, waiting {wait}s")
                        await asyncio.sleep(wait)
                    else:
                        logger.warning(f"HTTP {resp.status} for {url}")
                        return None
            except RuntimeError as e:
                if "Session is closed" in str(e):
                    logger.info("HTTP session closed (shutting down)")
                    return None
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"Request failed (attempt {attempt+1}): {e}")
                await asyncio.sleep(5 * (attempt + 1))
        return None

    # --- Gamma API: markets ---

    async def get_sports_events(self, limit: int = 100, offset: int = 0) -> list:
        """Fetch all active sports events, paginating until exhausted."""
        events = []
        while True:
            data = await self._get(
                f"{self.gamma_url}/events",
                params={
                    "tag": "sports",
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
            )
            if not data or len(data) == 0:
                break
            events.extend(data)
            if len(data) < limit:
                break
            offset += limit
        logger.info(f"Found {len(events)} sports events")
        return events

    def parse_event(self, event: dict) -> list[dict]:
        """Parse an event dict into a list of market dicts."""
        markets = []
        for market in event.get("markets", []):
            # Determine sport and league from tags
            sport = "unknown"
            league = "unknown"
            tags = [t.get("label", "").lower() for t in event.get("tags", [])]
            # Also try event-level fields
            event_sport = event.get("sport", "").lower()
            event_league = event.get("league", "").lower()

            if event_sport:
                sport = event_sport
            elif any(t in tags for t in ["basketball", "nba", "ncaa basketball"]):
                sport = "basketball"
            elif any(t in tags for t in ["football", "nfl", "soccer"]):
                sport = "football"
            elif any(t in tags for t in ["tennis", "atp", "wta"]):
                sport = "tennis"
            elif any(t in tags for t in ["baseball", "mlb"]):
                sport = "baseball"
            elif any(t in tags for t in ["hockey", "nhl"]):
                sport = "hockey"

            if event_league:
                league = event_league
            else:
                for t in tags:
                    if t in ["nba", "nfl", "mlb", "nhl", "atp", "wta", "ncaa",
                             "premier league", "la liga", "serie a", "bundesliga",
                             "champions league", "mls"]:
                        league = t
                        break

            # Tokens
            token_yes = None
            token_no = None
            raw_tokens = market.get("clobTokenIds", [])
            clob_tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
            if len(clob_tokens) >= 2:
                token_yes = clob_tokens[0]
                token_no = clob_tokens[1]
            elif len(clob_tokens) == 1:
                token_yes = clob_tokens[0]

            # Event start
            event_start = None
            end_date = event.get("endDate") or market.get("endDate")
            start_date = event.get("startDate") or market.get("startDate")
            game_start = event.get("gameStartTime") or market.get("gameStartTime")

            date_str = game_start or start_date or end_date
            if date_str:
                try:
                    event_start = datetime.fromisoformat(
                        date_str.replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    event_start = None

            if not event_start:
                continue

            markets.append(
                {
                    "id": market.get("id", ""),
                    "slug": market.get("slug") or event.get("slug", ""),
                    "question": market.get("question", ""),
                    "sport": sport,
                    "league": league,
                    "event_start": event_start,
                    "token_id_yes": token_yes,
                    "token_id_no": token_no,
                    "status": "active",
                    "volume_24h": float(market.get("volume24hr", 0) or 0),
                    "enable_order_book": bool(market.get("enableOrderBook", False)),
                }
            )
        return markets

    # --- CLOB API: orderbook ---

    async def get_orderbook(self, token_id: str) -> Optional[dict]:
        """Fetch the orderbook for a given token_id, applying sanity filters."""
        data = await self._get(
            f"{self.clob_url}/book", params={"token_id": token_id}
        )
        if not data:
            return None

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if not bids or not asks:
            return None

        # Polymarket CLOB: bids ascending → [-1] = best bid; asks descending → [-1] = best ask
        best_bid = float(bids[-1]["price"])
        best_ask = float(asks[-1]["price"])

        # Filter: spread > 90% means this is not a real market
        if best_ask - best_bid > 0.90:
            return None

        # Filter: price at extreme (<3¢ or >97¢) means market is effectively settled
        mid = (best_bid + best_ask) / 2
        if mid < 0.03 or mid > 0.97:
            return None

        # Depth = $ notional (size), not price*size for binary contracts
        bid_depth = sum(float(b["size"]) for b in bids)
        ask_depth = sum(float(a["size"]) for a in asks)

        spread = best_ask - best_bid

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "mid_price": mid,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "raw_bids": bids,
            "raw_asks": asks,
        }

    async def get_all_outcomes_orderbooks(self, market: dict) -> list[dict]:
        """Fetch orderbooks for both YES and NO outcomes of a market."""
        results = []
        for token_id in [market.get("token_id_yes"), market.get("token_id_no")]:
            if not token_id:
                continue
            ob = await self.get_orderbook(token_id)
            if ob:
                ob["token_id"] = token_id
                results.append(ob)
        return results

    # --- CLOB API: fee rate ---

    async def get_fee_rate(self, token_id: str) -> Optional[float]:
        """Fetch the current fee rate for a token_id."""
        data = await self._get(
            f"{self.clob_url}/fee-rate", params={"token_id": token_id}
        )
        if data and "fee_rate" in data:
            return float(data["fee_rate"])
        # Some markets return a different response format
        if data and "rate" in data:
            return float(data["rate"])
        return None

    # --- CLOB API: price history ---

    async def get_price_history(
        self, token_id: str, interval: str = "max", fidelity: int = 60
    ) -> Optional[list]:
        """
        Fetch price history for a token.
        interval: 1h, 6h, 1d, 1w, 1m, max
        fidelity: seconds between data points (60 = 1 min — better for newer markets)
        """
        # Primary endpoint: market=token_id, fidelity=60
        data = await self._get(
            f"{self.clob_url}/prices-history",
            params={
                "market": token_id,
                "interval": interval,
                "fidelity": fidelity,
            },
        )
        if data and "history" in data and len(data["history"]) > 0:
            return data["history"]

        # Fallback: smaller interval if max is empty (new market)
        data = await self._get(
            f"{self.clob_url}/prices-history",
            params={"market": token_id, "interval": "1w", "fidelity": 60},
        )
        if data and "history" in data and len(data["history"]) > 0:
            return data["history"]

        logger.debug(f"No price history for token {token_id[:16]}...")
        return None

    async def get_current_prices(self, token_id: str) -> Optional[dict]:
        """Fetch the current price via the book endpoint — at minimum mid price."""
        ob = await self.get_orderbook(token_id)
        if ob and ob.get("mid_price"):
            return {"mid_price": ob["mid_price"], "best_bid": ob["best_bid"], "best_ask": ob["best_ask"]}
        return None

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """Fetch the current midpoint price for a token."""
        data = await self._get(
            f"{self.clob_url}/midpoint", params={"token_id": token_id}
        )
        if data and "mid" in data:
            return float(data["mid"])
        return None

    async def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """Fetch the price of the most recent trade for a token."""
        data = await self._get(
            f"{self.clob_url}/last-trade-price", params={"token_id": token_id}
        )
        if data and "price" in data:
            return float(data["price"])
        return None
