"""
Unit tests for trading module pure functions.

Run with:
    pytest tests/test_trading.py -v

No external connections required — all tests are pure function calls.
"""

import pytest

from trading.entry_filter import check_entry, format_market_status
from trading.risk_manager import (
    can_open,
    max_position_usd,
    max_total_exposure_usd,
    position_size_by_ev,
    tanking_roi_estimate,
    within_slippage,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def config():
    return {
        "trading": {
            "enabled": True,
            "budget_usd": 100.0,
            "max_position_pct": 5.0,        # $5 max per trade
            "max_total_exposure_pct": 30.0,  # $30 max total
            "min_ask_depth_usd": 20.0,
            "price_slippage_tolerance": 0.03,
        }
    }


# ── entry_filter.check_entry ──────────────────────────────────────────────────

class TestCheckEntry:
    def test_liquid_market_enters(self):
        decision, reason, emoji = check_entry(
            bid=0.18, ask=0.20, signal_price=0.185,
            ask_depth_usd=200.0, hours_to_game=6.0,
        )
        assert decision == "enter"
        assert emoji == "🟢"

    def test_dead_ask_returns_limit_even_when_far_from_signal(self):
        """ask=0.99 on signal=0.185 → dead-market check fires FIRST (reordered
        post-2026-04-17 — see entry_filter docstring). Before the reorder, the
        ratio check (0.99 > 0.185*1.15=0.213) short-circuited and we skipped.
        After the reorder we place a GTC limit at signal_price and wait —
        which is the whole point of having a dead-ask branch.

        Regression: real-world MLB pre-game markets often have ask=0.99 as
        the only level on the book (placeholder, not a real trading price).
        Skipping these meant 100% of MLB HIGH signals never opened a paper
        position in dry_run mode; 2026-04-17 post-ship check found 0 live
        positions despite 5 HIGH+BUY signals in 24h."""
        decision, reason, emoji = check_entry(
            bid=0.01, ask=0.99, signal_price=0.185,
            ask_depth_usd=5.0, hours_to_game=6.0,
        )
        assert decision == "limit"
        assert emoji == "🟡"
        assert "dead market" in reason

    def test_dead_ask_on_realistic_mlb_signal_returns_limit(self):
        """Exact 2026-04-17 MLB scenario: ask=0.99 on signal=0.515 (Chicago
        Cubs HIGH). Dead-market check must fire, not ratio-check-skip."""
        decision, _, emoji = check_entry(
            bid=0.01, ask=0.99, signal_price=0.515,
            ask_depth_usd=0.0, hours_to_game=4.0,
        )
        assert decision == "limit"
        assert emoji == "🟡"

    def test_dead_ask_returns_limit_when_signal_near_one(self):
        """ask=0.95 on signal=0.90 → ratio is 0.95/0.90=1.055 < 1.15 → ratio OK, dead ask → limit."""
        decision, _, emoji = check_entry(
            bid=0.88, ask=0.95, signal_price=0.90,
            ask_depth_usd=50.0, hours_to_game=6.0,
        )
        assert decision == "limit"
        assert emoji == "🟡"

    def test_game_too_close_skips(self):
        """Game starts in 30 minutes — hard skip."""
        decision, reason, emoji = check_entry(
            bid=0.18, ask=0.20, signal_price=0.185,
            ask_depth_usd=200.0, hours_to_game=0.5,
        )
        assert decision == "skip"
        assert emoji == "🔴"
        assert "0.5h" in reason

    def test_ask_too_far_above_signal_skips(self):
        """Ask is 20% above signal — exceeds 15% tolerance."""
        decision, reason, emoji = check_entry(
            bid=0.20, ask=0.23, signal_price=0.185,
            ask_depth_usd=200.0, hours_to_game=6.0,
        )
        assert decision == "skip"
        assert emoji == "🔴"
        assert "signal" in reason

    def test_wide_spread_returns_limit(self):
        """bid=0.10, ask=0.32, signal=0.30 → ask within ratio (0.32 < 0.30*1.15=0.345),
        but spread=0.22 > 0.20 → limit."""
        decision, reason, emoji = check_entry(
            bid=0.10, ask=0.32, signal_price=0.30,
            ask_depth_usd=200.0, hours_to_game=6.0,
        )
        assert decision == "limit"
        assert emoji == "🟡"
        assert "spread" in reason

    def test_thin_depth_returns_limit(self):
        """Ask depth $5 is below min $20."""
        decision, reason, emoji = check_entry(
            bid=0.18, ask=0.20, signal_price=0.185,
            ask_depth_usd=5.0, hours_to_game=6.0,
            min_depth_usd=20.0,
        )
        assert decision == "limit"
        assert emoji == "🟡"
        assert "$5" in reason

    def test_no_bid_returns_limit(self):
        """bid=0 means no buyers."""
        decision, _, emoji = check_entry(
            bid=0.0, ask=0.20, signal_price=0.185,
            ask_depth_usd=200.0, hours_to_game=6.0,
        )
        assert decision == "limit"
        assert emoji == "🟡"

    def test_exactly_at_boundary_enters(self):
        """Exactly 1 hour to game — should still enter."""
        decision, _, _ = check_entry(
            bid=0.18, ask=0.20, signal_price=0.185,
            ask_depth_usd=200.0, hours_to_game=1.01,
        )
        assert decision == "enter"

    def test_exactly_below_boundary_skips(self):
        """Exactly 0.99 hours — should skip."""
        decision, _, _ = check_entry(
            bid=0.18, ask=0.20, signal_price=0.185,
            ask_depth_usd=200.0, hours_to_game=0.99,
        )
        assert decision == "skip"


class TestFormatMarketStatus:
    def test_returns_string_with_emoji(self):
        result = format_market_status(
            bid=0.18, ask=0.20, signal_price=0.185,
            ask_depth_usd=200.0, hours_to_game=6.0,
        )
        assert isinstance(result, str)
        assert any(e in result for e in ("🟢", "🟡", "🔴"))

    def test_dead_market_far_from_signal_shows_yellow(self):
        """ask=0.99 >> signal=0.185: post-2026-04-17 dead-market check runs
        first → yellow limit (was red skip pre-reorder)."""
        result = format_market_status(
            bid=0.01, ask=0.99, signal_price=0.185,
            ask_depth_usd=1.0, hours_to_game=6.0,
        )
        assert "🟡" in result

    def test_dead_market_near_signal_shows_yellow(self):
        """ask=0.95 near signal=0.90 → passes ratio, hits dead-ask → yellow."""
        result = format_market_status(
            bid=0.88, ask=0.95, signal_price=0.90,
            ask_depth_usd=50.0, hours_to_game=6.0,
        )
        assert "🟡" in result


# ── risk_manager ──────────────────────────────────────────────────────────────

class TestMaxPositionUsd:
    def test_basic(self, config):
        # budget=100, max_position_pct=5 → $5
        assert max_position_usd(config) == 5.0

    def test_scales_with_budget(self):
        cfg = {"trading": {"budget_usd": 500.0, "max_position_pct": 3.0, "max_total_exposure_pct": 30.0}}
        assert max_position_usd(cfg) == 15.0


class TestMaxTotalExposureUsd:
    def test_basic(self, config):
        # budget=100, max_total_exposure_pct=30 → $30
        assert max_total_exposure_usd(config) == 30.0


class TestCanOpen:
    def test_allows_when_under_limits(self, config):
        ok, reason = can_open(config, total_exposure=10.0, ask_depth_usd=100.0)
        assert ok is True
        assert reason == ""

    def test_blocks_when_exposure_maxed(self, config):
        ok, reason = can_open(config, total_exposure=30.0, ask_depth_usd=100.0)
        assert ok is False
        assert "max exposure" in reason

    def test_blocks_when_depth_too_thin(self, config):
        ok, reason = can_open(config, total_exposure=5.0, ask_depth_usd=10.0)
        assert ok is False
        assert "too thin" in reason

    def test_allows_at_exact_limit_minus_one_cent(self, config):
        ok, _ = can_open(config, total_exposure=29.99, ask_depth_usd=20.0)
        assert ok is True

    def test_blocks_at_exact_exposure_limit(self, config):
        ok, _ = can_open(config, total_exposure=30.0, ask_depth_usd=20.0)
        assert ok is False


class TestPositionSizeByEv:
    """config: budget=100, max_position_pct=5% → max=$5, ev_min_scale=0.4"""

    def test_min_roi_gives_min_scale(self, config):
        # roi=5% (floor) → 40% of $5 = $2.00
        size_usd, _ = position_size_by_ev(config, price=0.25, roi_pct=5.0)
        assert size_usd == pytest.approx(2.0, abs=0.01)

    def test_max_roi_gives_full_position(self, config):
        # roi=20% (ceiling) → 100% of $5 = $5.00
        size_usd, _ = position_size_by_ev(config, price=0.25, roi_pct=20.0)
        assert size_usd == pytest.approx(5.0, abs=0.01)

    def test_midpoint_roi_interpolates(self, config):
        # roi=12.5% (midpoint of 5→20) → 70% of $5 = $3.50
        size_usd, _ = position_size_by_ev(config, price=0.25, roi_pct=12.5)
        assert size_usd == pytest.approx(3.5, abs=0.02)

    def test_above_max_roi_is_capped(self, config):
        # roi=50% → still capped at $5.00
        size_usd, _ = position_size_by_ev(config, price=0.25, roi_pct=50.0)
        assert size_usd == pytest.approx(5.0, abs=0.01)

    def test_below_min_roi_gives_min_scale(self, config):
        # roi=1% (below floor of 5%) → 40% of $5 = $2.00
        size_usd, _ = position_size_by_ev(config, price=0.25, roi_pct=1.0)
        assert size_usd == pytest.approx(2.0, abs=0.01)

    def test_shares_calculated_from_size_usd(self, config):
        # roi=20% → $5.00 / price=0.50 = 10 shares
        _, size_shares = position_size_by_ev(config, price=0.50, roi_pct=20.0)
        assert size_shares == pytest.approx(10.0, rel=0.01)

    def test_zero_price_returns_zero_shares(self, config):
        _, size_shares = position_size_by_ev(config, price=0.0, roi_pct=15.0)
        assert size_shares == 0.0


class TestTankingRoiEstimate:
    def test_moderate_signal(self):
        # diff=0.5, no drift → 0.5*15 = 7.5%
        roi = tanking_roi_estimate(motivation_differential=0.5, actual_drift=None)
        assert roi == pytest.approx(7.5)

    def test_strong_signal(self):
        # diff=1.0 → 15%
        roi = tanking_roi_estimate(motivation_differential=1.0, actual_drift=None)
        assert roi == pytest.approx(15.0)

    def test_drift_bonus_added(self):
        # diff=0.5 (7.5%) + drift=0.03 (0.03*20=0.6%) = 8.1%
        roi = tanking_roi_estimate(motivation_differential=0.5, actual_drift=0.03)
        assert roi == pytest.approx(8.1)

    def test_negative_drift_no_bonus(self):
        # price moving against us → no bonus
        roi = tanking_roi_estimate(motivation_differential=0.5, actual_drift=-0.05)
        assert roi == pytest.approx(7.5)

    def test_capped_at_30_pct(self):
        # very strong signal shouldn't exceed 30%
        roi = tanking_roi_estimate(motivation_differential=3.0, actual_drift=0.5)
        assert roi == 30.0


class TestWithinSlippage:
    def test_within_tolerance(self, config):
        # signal=0.185, live=0.210, tolerance=0.03 → 0.185+0.03=0.215 >= 0.210 ✓
        assert within_slippage(config, signal_price=0.185, live_price=0.210) is True

    def test_exactly_at_limit(self, config):
        assert within_slippage(config, signal_price=0.185, live_price=0.215) is True

    def test_exceeds_tolerance(self, config):
        assert within_slippage(config, signal_price=0.185, live_price=0.216) is False

    def test_live_below_signal_is_fine(self, config):
        assert within_slippage(config, signal_price=0.185, live_price=0.10) is True


# ── entry_filter edge cases ───────────────────────────────────────────────────

class TestCheckEntryEdgeCases:
    def test_all_zeros_returns_limit(self):
        """No market data at all — default to limit order."""
        decision, _, _ = check_entry(
            bid=0.0, ask=0.0, signal_price=0.185,
            ask_depth_usd=0.0, hours_to_game=6.0,
        )
        # ask=0.0 < 0.95, bid=0.0 triggers no-bid check
        assert decision == "limit"

    def test_ask_just_under_ratio_limit_enters(self):
        """ask < signal*1.15 → passes ratio check, liquid → enter."""
        signal = 0.200
        ask = round(signal * 1.14, 4)   # 0.228 — safely under 1.15 boundary
        decision, _, _ = check_entry(
            bid=0.22, ask=ask, signal_price=signal,
            ask_depth_usd=200.0, hours_to_game=6.0,
        )
        assert decision == "enter"

    def test_ask_just_over_ratio_limit_skips(self):
        """ask > signal*1.15 → skip regardless of other conditions."""
        signal = 0.200
        ask = round(signal * 1.16, 4)   # 0.232 — over the 1.15 limit
        decision, reason, _ = check_entry(
            bid=0.22, ask=ask, signal_price=signal,
            ask_depth_usd=200.0, hours_to_game=6.0,
        )
        assert decision == "skip"
        assert "signal" in reason
