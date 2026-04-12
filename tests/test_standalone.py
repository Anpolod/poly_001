"""Standalone tests — pure cost math, no external dependencies"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def compute_spread_pct(best_bid, best_ask):
    if not best_bid or not best_ask or best_bid <= 0:
        return None
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return None
    return round((best_ask - best_bid) / mid * 100, 4)


def compute_taker_round_trip(fee_rate, spread_pct, est_slippage_pct):
    return (fee_rate * 100 * 2) + spread_pct + est_slippage_pct


def compute_maker_round_trip(spread_pct, adverse_selection_mult, fee_rate, rebate_pct):
    adverse_selection = spread_pct * adverse_selection_mult
    rebate = fee_rate * 100 * (rebate_pct / 100)
    return adverse_selection - rebate


def compute_ratio(move, mid_price, cost_pct):
    if move is None or mid_price is None or mid_price <= 0 or cost_pct <= 0:
        return None
    move_pct = (move / mid_price) * 100
    return round(move_pct / cost_pct, 2)


def compute_verdict(ratio, go_threshold, marginal_threshold):
    if ratio is None:
        return "NO_DATA"
    if ratio >= go_threshold:
        return "GO"
    if ratio >= marginal_threshold:
        return "MARGINAL"
    return "NO_GO"


# === TESTS ===

def test_spread_pct():
    result = compute_spread_pct(0.47, 0.49)
    assert abs(result - 4.1667) < 0.01


def test_spread_pct_tight():
    result = compute_spread_pct(0.50, 0.51)
    assert abs(result - 1.9802) < 0.01


def test_taker_round_trip():
    cost = compute_taker_round_trip(0.0075, 4.17, 0.5)
    assert abs(cost - 6.17) < 0.01


def test_taker_tight_spread():
    cost = compute_taker_round_trip(0.0075, 1.98, 0.5)
    assert abs(cost - 3.98) < 0.01


def test_maker_round_trip():
    cost = compute_maker_round_trip(4.17, 1.5, 0.0075, 25)
    expected = 4.17 * 1.5 - 0.0075 * 100 * 0.25
    assert abs(cost - expected) < 0.01


def test_ratio_go():
    ratio = compute_ratio(0.05, 0.50, 4.0)
    assert abs(ratio - 2.5) < 0.01
    assert compute_verdict(ratio, 2.0, 1.5) == "GO"


def test_ratio_marginal():
    ratio = compute_ratio(0.03, 0.50, 4.0)
    assert abs(ratio - 1.5) < 0.01
    assert compute_verdict(ratio, 2.0, 1.5) == "MARGINAL"


def test_ratio_no_go():
    ratio = compute_ratio(0.02, 0.50, 5.0)
    assert abs(ratio - 0.8) < 0.01
    assert compute_verdict(ratio, 2.0, 1.5) == "NO_GO"


def test_ratio_no_data():
    ratio = compute_ratio(None, 0.50, 5.0)
    assert compute_verdict(ratio, 2.0, 1.5) == "NO_DATA"


def test_real_scenario_nba():
    """Realistic NBA moneyline liquid market — 4¢ move over 24h"""
    bid, ask = 0.62, 0.64
    spread_pct = compute_spread_pct(bid, ask)
    taker_cost = compute_taker_round_trip(0.0075, spread_pct, 0.5)
    ratio = compute_ratio(0.04, (bid + ask) / 2, taker_cost)
    verdict = compute_verdict(ratio, 2.0, 1.5)
    assert verdict in ("GO", "MARGINAL", "NO_GO")


def test_real_scenario_thin_market():
    """Thin market with large spread must be rejected"""
    bid, ask = 0.35, 0.42
    spread_pct = compute_spread_pct(bid, ask)
    taker_cost = compute_taker_round_trip(0.0075, spread_pct, 1.0)
    ratio = compute_ratio(0.05, (bid + ask) / 2, taker_cost)
    assert compute_verdict(ratio, 2.0, 1.5) == "NO_GO"
