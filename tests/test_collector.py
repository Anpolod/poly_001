"""Tests for collector modules: normalizer, market_discovery, rest_client"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from collector.market_discovery import MarketDiscovery, detect_sport
from collector.normalizer import (
    compute_price_move,
    compute_spread_pct,
    compute_time_to_event,
    normalize_snapshot,
)
from collector.rest_client import RestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _future(hours=24):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _past(hours=24):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _make_config(**overrides):
    cfg = {
        "api": {
            "gamma_base_url": "https://gamma-api.polymarket.com",
            "clob_base_url": "https://clob.polymarket.com",
            "ws_url": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            "request_delay_sec": 0,
        },
        "phase0": {"min_volume_24h": 5000},
        "phase1": {"min_volume_24h": 10000, "max_spread": 0.03, "min_depth": 1000},
        "filter_tradeable": {
            "min_volume_24h": 10000,
            "max_spread": 0.03,
            "min_depth": 1000,
            "mid_extreme_low": 0.15,
            "mid_extreme_high": 0.85,
            "mid_favorite": 0.80,
        },
        "collector": {
            "ws_reconnect_base_delay": 5,
            "ws_reconnect_max_delay": 60,
            "ws_reconnect_backoff": 2.0,
            "ws_silence_threshold_sec": 60,
            "heartbeat_interval": 60,
            "gap_threshold_minutes": 5,
        },
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# normalizer.py
# ---------------------------------------------------------------------------

class TestComputeSpreadPct:
    def test_normal(self):
        result = compute_spread_pct(0.47, 0.49)
        assert abs(result - 4.1667) < 0.01

    def test_tight_spread(self):
        result = compute_spread_pct(0.50, 0.51)
        assert abs(result - 1.9802) < 0.01

    def test_zero_bid(self):
        assert compute_spread_pct(0, 0.49) is None

    def test_none_bid(self):
        assert compute_spread_pct(None, 0.49) is None

    def test_equal_bid_ask(self):
        result = compute_spread_pct(0.50, 0.50)
        assert result == 0.0


class TestComputePriceMove:
    def _make_history_tp(self, prices_and_offsets):
        """Build t/p dict history from (hours_ago, price) pairs."""
        now = datetime.now(timezone.utc).timestamp()
        return [{"t": now - h * 3600, "p": p} for h, p in prices_and_offsets]

    def test_tp_format(self):
        history = self._make_history_tp([(48, 0.40), (24, 0.45), (0.1, 0.50)])
        move = compute_price_move(history, 24)
        assert abs(move - 0.05) < 0.005  # 0.50 - 0.45 = 0.05

    def test_timestamp_price_format(self):
        now = datetime.now(timezone.utc).timestamp()
        # Need a point near the 24h target; without it the nearest point to
        # (now - 24h) would be the most-recent one, giving move=0.
        history = [
            {"timestamp": now - 48 * 3600, "price": 0.40},
            {"timestamp": now - 25 * 3600, "price": 0.44},  # closest to 24h ago
            {"timestamp": now - 0.1 * 3600, "price": 0.55},
        ]
        move = compute_price_move(history, 24)
        # current=0.55, closest-to-24h=0.44 → |0.55-0.44|=0.11
        assert move is not None
        assert abs(move - 0.11) < 0.01

    def test_list_tuple_format(self):
        now = datetime.now(timezone.utc).timestamp()
        history = [
            [now - 48 * 3600, 0.40],
            [now - 25 * 3600, 0.40],  # closest to 24h ago
            [now - 0.1 * 3600, 0.60],
        ]
        move = compute_price_move(history, 24)
        # current=0.60, closest-to-24h=0.40 → |0.60-0.40|=0.20
        assert abs(move - 0.20) < 0.01

    def test_insufficient_data(self):
        assert compute_price_move([], 24) is None
        assert compute_price_move([{"t": 1, "p": 0.5}], 24) is None

    def test_no_valid_points(self):
        assert compute_price_move([{"t": 0, "p": 0}, {"t": 0, "p": 0}], 24) is None


class TestNormalizeSnapshot:
    def test_field_mapping(self):
        event_start = _future(10)
        ob = {
            "best_bid": 0.47, "best_ask": 0.49, "mid_price": 0.48,
            "spread": 0.02, "bid_depth": 1500, "ask_depth": 1200,
            "volume_24h": 50000,
        }
        snap = normalize_snapshot("market-1", ob, event_start)

        assert snap["market_id"] == "market-1"
        assert snap["best_bid"] == 0.47
        assert snap["best_ask"] == 0.49
        assert snap["mid_price"] == 0.48
        assert snap["spread"] == 0.02
        assert snap["bid_depth"] == 1500
        assert snap["volume_24h"] == 50000
        assert snap["time_to_event_h"] > 0
        assert isinstance(snap["ts"], datetime)


class TestComputeTimeToEvent:
    def test_future_event(self):
        event_start = _future(10)
        tte = compute_time_to_event(event_start)
        assert 9.9 < tte < 10.1

    def test_past_event(self):
        event_start = _past(5)
        tte = compute_time_to_event(event_start)
        assert tte < 0


# ---------------------------------------------------------------------------
# market_discovery.py — detect_sport
# ---------------------------------------------------------------------------

class TestDetectSport:
    @pytest.mark.parametrize("slug,expected", [
        ("nba-det-phi-2026-04-04", "basketball"),
        ("nhl-bos-tor-2026-04-05", "hockey"),
        ("mlb-nyy-bos-2026-04-06", "baseball"),
        ("nfl-kc-sf-2026-02-02", "football"),
        ("atp-djokovic-alcaraz", "tennis"),
        ("wta-swiatek-gauff", "tennis"),
        ("epl-mnu-liv-2026", "football"),
        ("ucl-rea-bay-2026", "football"),
        ("cbb-duke-unc-2026", "basketball"),
        ("random-market-slug", "unknown"),
    ])
    def test_slug_prefix(self, slug, expected):
        assert detect_sport(slug) == expected


# ---------------------------------------------------------------------------
# market_discovery.py — MarketDiscovery
# ---------------------------------------------------------------------------

class TestIsSportsMarket:
    def setup_method(self):
        self.disc = MarketDiscovery(MagicMock(), _make_config())

    def test_nba_league(self):
        assert self.disc.is_sports_market({"slug": "nba-det-phi", "league": "nba", "sport": ""})

    def test_nfl_league(self):
        assert self.disc.is_sports_market({"slug": "nfl-kc-sf", "league": "nfl", "sport": ""})

    def test_basketball_sport(self):
        assert self.disc.is_sports_market({"slug": "some-slug", "league": "", "sport": "basketball"})

    def test_blocked_by_blacklist(self):
        assert not self.disc.is_sports_market({"slug": "trump-wins-election", "league": "nba", "sport": ""})

    def test_slug_prefix_match(self):
        assert self.disc.is_sports_market({"slug": "nba-det-phi-2026", "league": "", "sport": ""})

    def test_unknown_market(self):
        assert not self.disc.is_sports_market({"slug": "weather-new-york", "league": "", "sport": ""})


class TestComputeLiquidityMetrics:
    def setup_method(self):
        self.disc = MarketDiscovery(MagicMock(), _make_config())

    def test_normal_orderbook(self):
        ob = {"best_bid": 0.47, "best_ask": 0.49, "bid_depth": 2000, "ask_depth": 1800}
        m = self.disc.compute_liquidity_metrics(ob)
        assert abs(m["spread"] - 0.02) < 0.001
        assert abs(m["mid_price"] - 0.48) < 0.001
        assert m["bid_depth_usd"] == 2000
        assert m["ask_depth_usd"] == 1800
        assert m["spread_pct"] > 0

    def test_zero_prices(self):
        ob = {"best_bid": 0, "best_ask": 0, "bid_depth": 0, "ask_depth": 0}
        m = self.disc.compute_liquidity_metrics(ob)
        assert m["mid_price"] == 0.0
        assert m["spread_pct"] == 0.0


class TestFilterTradeable:
    def setup_method(self):
        self.disc = MarketDiscovery(MagicMock(), _make_config())

    def _market(self, **kwargs):
        base = {
            "id": "m1", "slug": "nba-det-phi-2026-04-10",
            "sport": "basketball", "league": "nba",
            "event_start": _future(20),
            "token_id_yes": "tok1", "token_id_no": "tok2",
            "volume_24h": 50000,
        }
        base.update(kwargs)
        return base

    def _ob(self, bid=0.47, ask=0.49, depth=2000):
        return {"best_bid": bid, "best_ask": ask,
                "bid_depth": depth, "ask_depth": depth}

    def test_passes_all_filters(self):
        m = self._market()
        result = self.disc.filter_tradeable([m], {"m1": self._ob()})
        assert len(result) == 1
        assert result[0].verdict == "TRADEABLE"

    def test_filtered_low_volume(self):
        m = self._market(volume_24h=100)
        result = self.disc.filter_tradeable([m], {"m1": self._ob()})
        assert len(result) == 0

    def test_filtered_no_orderbook(self):
        m = self._market()
        result = self.disc.filter_tradeable([m], {})  # no OB
        assert len(result) == 0

    def test_filtered_wide_spread(self):
        m = self._market()
        result = self.disc.filter_tradeable([m], {"m1": self._ob(bid=0.40, ask=0.55)})
        assert len(result) == 0

    def test_filtered_low_depth(self):
        m = self._market()
        result = self.disc.filter_tradeable([m], {"m1": self._ob(depth=100)})
        assert len(result) == 0

    def test_filtered_extreme_low_odds(self):
        m = self._market()
        # mid = 0.10 < 0.15 → filtered
        result = self.disc.filter_tradeable([m], {"m1": self._ob(bid=0.09, ask=0.11)})
        assert len(result) == 0

    def test_filtered_extreme_high_odds(self):
        m = self._market()
        # mid = 0.90 > 0.85 → filtered
        result = self.disc.filter_tradeable([m], {"m1": self._ob(bid=0.89, ask=0.91)})
        assert len(result) == 0

    def test_filtered_strong_favorite(self):
        m = self._market()
        # mid = 0.82 > 0.80 → strong favorite
        result = self.disc.filter_tradeable([m], {"m1": self._ob(bid=0.81, ask=0.83)})
        assert len(result) == 0

    def test_filtered_unknown_sport(self):
        m = self._market(slug="crypto-btc-price", sport="unknown")
        result = self.disc.filter_tradeable([m], {"m1": self._ob()})
        assert len(result) == 0

    def test_sport_resolved_from_slug(self):
        # sport field is wrong but slug has valid prefix
        m = self._market(slug="nba-det-phi-2026", sport="unknown")
        result = self.disc.filter_tradeable([m], {"m1": self._ob()})
        assert len(result) == 1


# ---------------------------------------------------------------------------
# rest_client.py — parse_event (pure, no HTTP)
# ---------------------------------------------------------------------------

class TestParseEvent:
    def setup_method(self):
        cfg = _make_config()
        cfg["api"]["request_delay_sec"] = 0
        self.client = RestClient(cfg)

    def _event(self, **kwargs):
        base = {
            "slug": "nba-det-phi-2026-04-10",
            "startDate": "2026-04-10T19:00:00Z",
            "tags": [{"label": "NBA"}],
            "sport": "basketball",
            "league": "nba",
            "markets": [],
        }
        base.update(kwargs)
        return base

    def _market(self, **kwargs):
        base = {
            "id": "mkt-1",
            "slug": "nba-det-phi-2026-04-10",
            "question": "Will Detroit win?",
            "clobTokenIds": '["tok_yes", "tok_no"]',
            "enableOrderBook": True,
            "volume24hr": "25000",
        }
        base.update(kwargs)
        return base

    def test_parses_basic_market(self):
        event = self._event(markets=[self._market()])
        result = self.client.parse_event(event)
        assert len(result) == 1
        m = result[0]
        assert m["id"] == "mkt-1"
        assert m["sport"] == "basketball"
        assert m["token_id_yes"] == "tok_yes"
        assert m["token_id_no"] == "tok_no"
        assert m["volume_24h"] == 25000.0

    def test_skips_market_without_valid_date(self):
        market = self._market()
        market.pop("startDate", None)
        event = self._event(markets=[market], startDate=None, gameStartTime=None)
        result = self.client.parse_event(event)
        assert len(result) == 0

    def test_token_ids_as_list(self):
        market = self._market(clobTokenIds=["tok_a", "tok_b"])
        result = self.client.parse_event(self._event(markets=[market]))
        assert result[0]["token_id_yes"] == "tok_a"
        assert result[0]["token_id_no"] == "tok_b"

    def test_single_token(self):
        market = self._market(clobTokenIds='["only_yes"]')
        result = self.client.parse_event(self._event(markets=[market]))
        assert result[0]["token_id_yes"] == "only_yes"
        assert result[0]["token_id_no"] is None

    def test_sport_from_tags_fallback(self):
        event = self._event(sport="", markets=[self._market()])
        event["tags"] = [{"label": "NBA"}, {"label": "basketball"}]
        result = self.client.parse_event(event)
        assert result[0]["sport"] == "basketball"

    def test_multiple_markets_in_event(self):
        m1 = self._market(id="m1", question="Will Detroit win?")
        m2 = self._market(id="m2", question="Total points over/under?")
        result = self.client.parse_event(self._event(markets=[m1, m2]))
        assert len(result) == 2


# ---------------------------------------------------------------------------
# rest_client.py — get_orderbook (HTTP mock)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGetOrderbook:
    def setup_method(self):
        from aioresponses import aioresponses as aiorm
        self._aiorm = aiorm

    async def _client(self):
        cfg = _make_config()
        cfg["api"]["request_delay_sec"] = 0
        c = RestClient(cfg)
        await c.start()
        return c

    _TOKEN = "tok1"
    _BOOK_URL = f"https://clob.polymarket.com/book?token_id={_TOKEN}"

    async def _run(self, mock_setup_fn):
        """Helper: create client, apply mock, call get_orderbook, close."""
        cfg = _make_config()
        cfg["api"]["request_delay_sec"] = 0
        c = RestClient(cfg)
        with self._aiorm() as m:
            mock_setup_fn(m)
            await c.start()
            ob = await c.get_orderbook(self._TOKEN)
            await c.close()
        return ob

    async def test_valid_orderbook(self):
        def setup(m):
            m.get(
                self._BOOK_URL,

                payload={
                    "bids": [{"price": "0.44", "size": "500"}, {"price": "0.47", "size": "1000"}],
                    "asks": [{"price": "0.53", "size": "800"}, {"price": "0.49", "size": "1200"}],
                },
            )
        ob = await self._run(setup)
        assert ob is not None
        assert abs(ob["best_bid"] - 0.47) < 0.001
        assert abs(ob["best_ask"] - 0.49) < 0.001
        assert abs(ob["spread"] - 0.02) < 0.001

    async def test_wide_spread_filtered(self):
        # bids[-1]=0.05, asks[-1]=0.98 → spread=0.93 > 0.90 → None
        def setup(m):
            m.get(
                self._BOOK_URL,

                payload={
                    "bids": [{"price": "0.01", "size": "10"}, {"price": "0.05", "size": "10"}],
                    "asks": [{"price": "0.99", "size": "10"}, {"price": "0.98", "size": "10"}],
                },
            )
        assert await self._run(setup) is None

    async def test_extreme_mid_filtered(self):
        # mid = (0.01 + 0.02) / 2 = 0.015 < 0.03 → None
        def setup(m):
            m.get(
                self._BOOK_URL,

                payload={
                    "bids": [{"price": "0.005", "size": "10"}, {"price": "0.01", "size": "10"}],
                    "asks": [{"price": "0.025", "size": "10"}, {"price": "0.02", "size": "10"}],
                },
            )
        assert await self._run(setup) is None

    async def test_http_429_returns_none_after_retries(self):
        def setup(m):
            for _ in range(3):
                m.get(self._BOOK_URL, status=429)
        assert await self._run(setup) is None

    async def test_empty_bids_or_asks_filtered(self):
        def setup(m):
            m.get(self._BOOK_URL, payload={"bids": [], "asks": []})
        assert await self._run(setup) is None
