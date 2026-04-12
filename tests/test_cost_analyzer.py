"""Tests for analytics/cost_analyzer.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics.cost_analyzer import (
    analyze_market,
    compute_maker_round_trip,
    compute_ratio,
    compute_taker_round_trip,
    compute_verdict,
)


def test_taker_round_trip():
    # fee=0.75%, spread=3%, slippage=0.5% → 1.5 + 3 + 0.5 = 5.0
    cost = compute_taker_round_trip(0.0075, 3.0, 0.5)
    assert abs(cost - 5.0) < 0.01


def test_maker_round_trip():
    # spread=3%, AS_mult=1.5, fee=0.75%, rebate=25%
    # AS = 4.5, rebate = 0.1875 → 4.3125
    cost = compute_maker_round_trip(3.0, 1.5, 0.0075, 25)
    expected = 3.0 * 1.5 - 0.0075 * 100 * 0.25
    assert abs(cost - expected) < 0.01


def test_ratio():
    # move=0.03, mid=0.50 → move_pct=6%, cost=5% → ratio=1.2
    ratio = compute_ratio(0.03, 0.50, 5.0)
    assert abs(ratio - 1.2) < 0.01


def test_ratio_none_inputs():
    assert compute_ratio(None, 0.50, 5.0) is None
    assert compute_ratio(0.03, None, 5.0) is None
    assert compute_ratio(0.03, 0.50, 0) is None


def test_verdict_all_cases():
    assert compute_verdict(2.5, 2.0, 1.5) == "GO"
    assert compute_verdict(1.7, 2.0, 1.5) == "MARGINAL"
    assert compute_verdict(1.0, 2.0, 1.5) == "NO_GO"
    assert compute_verdict(None, 2.0, 1.5) == "NO_DATA"


def test_analyze_market():
    market = {
        "id": "test-123",
        "slug": "test-match",
        "sport": "basketball",
        "league": "nba",
        "event_start": "2026-04-10T19:00:00+00:00",
        "volume_24h": 25000,
    }
    orderbook = {
        "best_bid": 0.47,
        "best_ask": 0.49,
        "spread": 0.02,
        "mid_price": 0.48,
        "bid_depth": 3200,
        "ask_depth": 2800,
    }
    config = {
        "phase0": {
            "est_slippage_pct": 0.5,
            "adverse_selection_mult": 1.5,
            "maker_rebate_pct": 25,
            "ratio_go_threshold": 2.0,
            "ratio_marginal_threshold": 1.5,
        }
    }

    result = analyze_market(market, orderbook, 0.0075, None, config)

    assert result["market_id"] == "test-123"
    assert result["spread"] == 0.02
    assert result["taker_rt_cost"] > 0
    assert result["maker_rt_cost"] > 0
    assert result["verdict"] in ("GO", "MARGINAL", "NO_GO", "NO_DATA")
