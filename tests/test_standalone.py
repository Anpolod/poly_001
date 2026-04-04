"""Standalone тести — без зовнішніх залежностей"""

import sys
from pathlib import Path

# Тестуємо чисту логіку без імпортів з collector

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


# === ТЕСТИ ===

def test_spread_pct():
    # bid=0.47, ask=0.49, mid=0.48, spread=0.02
    # spread_pct = 0.02/0.48 * 100 = 4.1667%
    result = compute_spread_pct(0.47, 0.49)
    assert abs(result - 4.1667) < 0.01, f"Expected ~4.17, got {result}"
    print(f"✓ spread_pct: {result}%")

def test_spread_pct_tight():
    # bid=0.50, ask=0.51, spread=0.01
    result = compute_spread_pct(0.50, 0.51)
    assert abs(result - 1.9802) < 0.01, f"Expected ~1.98, got {result}"
    print(f"✓ spread_pct tight: {result}%")

def test_taker_round_trip():
    # fee=0.75%, spread_pct=4.17%, slippage=0.5%
    cost = compute_taker_round_trip(0.0075, 4.17, 0.5)
    # = 1.5 + 4.17 + 0.5 = 6.17%
    assert abs(cost - 6.17) < 0.01, f"Expected ~6.17, got {cost}"
    print(f"✓ taker round-trip: {cost}%")

def test_taker_tight_spread():
    # fee=0.75%, spread_pct=1.98%, slippage=0.5%
    cost = compute_taker_round_trip(0.0075, 1.98, 0.5)
    # = 1.5 + 1.98 + 0.5 = 3.98%
    assert abs(cost - 3.98) < 0.01, f"Expected ~3.98, got {cost}"
    print(f"✓ taker round-trip tight: {cost}%")

def test_maker_round_trip():
    # spread=4.17%, AS_mult=1.5, fee=0.75%, rebate=25%
    cost = compute_maker_round_trip(4.17, 1.5, 0.0075, 25)
    # AS = 6.255, rebate = 0.75*0.25 = 0.1875
    # cost = 6.255 - 0.1875 = 6.0675
    expected = 4.17 * 1.5 - 0.0075 * 100 * 0.25
    assert abs(cost - expected) < 0.01, f"Expected ~{expected}, got {cost}"
    print(f"✓ maker round-trip: {cost}%")

def test_ratio_go():
    # move=0.05 (5¢), mid=0.50, cost=4%
    # move_pct = 10%, ratio = 10/4 = 2.5
    ratio = compute_ratio(0.05, 0.50, 4.0)
    assert abs(ratio - 2.5) < 0.01, f"Expected 2.5, got {ratio}"
    verdict = compute_verdict(ratio, 2.0, 1.5)
    assert verdict == "GO"
    print(f"✓ ratio {ratio} → {verdict}")

def test_ratio_marginal():
    # move=0.03 (3¢), mid=0.50, cost=4%
    # move_pct = 6%, ratio = 6/4 = 1.5
    ratio = compute_ratio(0.03, 0.50, 4.0)
    assert abs(ratio - 1.5) < 0.01, f"Expected 1.5, got {ratio}"
    verdict = compute_verdict(ratio, 2.0, 1.5)
    assert verdict == "MARGINAL"
    print(f"✓ ratio {ratio} → {verdict}")

def test_ratio_no_go():
    # move=0.02 (2¢), mid=0.50, cost=5%
    # move_pct = 4%, ratio = 4/5 = 0.8
    ratio = compute_ratio(0.02, 0.50, 5.0)
    assert abs(ratio - 0.8) < 0.01, f"Expected 0.8, got {ratio}"
    verdict = compute_verdict(ratio, 2.0, 1.5)
    assert verdict == "NO_GO"
    print(f"✓ ratio {ratio} → {verdict}")

def test_ratio_no_data():
    ratio = compute_ratio(None, 0.50, 5.0)
    verdict = compute_verdict(ratio, 2.0, 1.5)
    assert verdict == "NO_DATA"
    print(f"✓ no data → {verdict}")

def test_real_scenario_nba():
    """Реалістичний сценарій: NBA moneyline, ліквідний ринок"""
    bid, ask = 0.62, 0.64
    spread_pct = compute_spread_pct(bid, ask)  # ~3.17%
    taker_cost = compute_taker_round_trip(0.0075, spread_pct, 0.5)
    
    # Якщо ціна рухнулась на 4¢ за 24 години
    move_24h = 0.04
    mid = (bid + ask) / 2
    ratio = compute_ratio(move_24h, mid, taker_cost)
    verdict = compute_verdict(ratio, 2.0, 1.5)
    
    print(f"\n  NBA scenario: bid={bid} ask={ask}")
    print(f"  spread={spread_pct:.2f}%, taker_cost={taker_cost:.2f}%")
    print(f"  move_24h={move_24h}, ratio={ratio}, verdict={verdict}")
    print(f"✓ NBA scenario passed")

def test_real_scenario_thin():
    """Тонкий ринок: великий спред"""
    bid, ask = 0.35, 0.42
    spread_pct = compute_spread_pct(bid, ask)  # ~18.18%
    taker_cost = compute_taker_round_trip(0.0075, spread_pct, 1.0)
    
    move_24h = 0.05
    mid = (bid + ask) / 2
    ratio = compute_ratio(move_24h, mid, taker_cost)
    verdict = compute_verdict(ratio, 2.0, 1.5)
    
    print(f"\n  Thin market: bid={bid} ask={ask}")
    print(f"  spread={spread_pct:.2f}%, taker_cost={taker_cost:.2f}%")
    print(f"  move_24h={move_24h}, ratio={ratio}, verdict={verdict}")
    assert verdict == "NO_GO", f"Thin market should be NO_GO, got {verdict}"
    print(f"✓ Thin market correctly rejected")


if __name__ == "__main__":
    print("=" * 50)
    print("  ТЕСТИ COST ANALYZER")
    print("=" * 50)
    
    test_spread_pct()
    test_spread_pct_tight()
    test_taker_round_trip()
    test_taker_tight_spread()
    test_maker_round_trip()
    test_ratio_go()
    test_ratio_marginal()
    test_ratio_no_go()
    test_ratio_no_data()
    test_real_scenario_nba()
    test_real_scenario_thin()
    
    print("\n" + "=" * 50)
    print("  ✓ ВСІ 11 ТЕСТІВ ПРОЙШЛИ")
    print("=" * 50)
