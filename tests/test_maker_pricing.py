"""T-58: unit tests for trading/maker_pricing.py — pure math, no mocks."""

from __future__ import annotations

import math

import pytest

from trading.maker_pricing import (
    compute_quote_prices,
    needs_reprice,
    quote_is_within_rewards_envelope,
    size_from_capital,
)

# ── compute_quote_prices ──────────────────────────────────────────────────────

class TestComputeQuotePrices:
    def test_symmetric_around_mid(self) -> None:
        bid, ask = compute_quote_prices(mid=0.5, target_half_spread_pct=0.5, max_spread_pct=3.5)
        # 0.5% of 0.5 = 0.0025 → bid 0.497(5), ask 0.502(5). Floor/ceil to 0.001 tick.
        assert bid == 0.497      # floor(0.4975 * 1000) / 1000
        assert ask == 0.503      # ceil(0.5025 * 1000) / 1000

    def test_target_spread_clamped_to_max(self) -> None:
        """If target > max, the agent uses max (never quotes outside rewards envelope)."""
        bid, ask = compute_quote_prices(mid=0.5, target_half_spread_pct=10.0, max_spread_pct=3.0)
        # 3% of 0.5 = 0.015 → bid 0.485, ask 0.515
        assert bid == 0.485
        assert ask == 0.515

    def test_favorite_market(self) -> None:
        """At mid=0.90, 0.5% delta = 0.0045."""
        bid, ask = compute_quote_prices(mid=0.90, target_half_spread_pct=0.5, max_spread_pct=3.5)
        assert bid == 0.895
        assert ask == 0.905

    def test_longshot_market(self) -> None:
        """At mid=0.05, 0.5% delta = 0.00025. Floor/ceil to 0.001 tick:
        bid rounds DOWN to 0.049, ask rounds UP to 0.051."""
        bid, ask = compute_quote_prices(mid=0.05, target_half_spread_pct=0.5, max_spread_pct=3.5)
        assert bid == 0.049
        assert ask == 0.051

    def test_bid_never_below_0_001(self) -> None:
        """Polymarket CLOB rejects prices below 0.001. Guard should clamp."""
        # Construct a scenario where raw bid would go negative
        bid, _ = compute_quote_prices(mid=0.002, target_half_spread_pct=50.0, max_spread_pct=200.0)
        assert bid >= 0.001

    def test_ask_never_above_0_999(self) -> None:
        _, ask = compute_quote_prices(mid=0.998, target_half_spread_pct=50.0, max_spread_pct=200.0)
        assert ask <= 0.999

    def test_invalid_mid_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_quote_prices(mid=0.0, target_half_spread_pct=0.5, max_spread_pct=3.5)
        with pytest.raises(ValueError):
            compute_quote_prices(mid=1.0, target_half_spread_pct=0.5, max_spread_pct=3.5)
        with pytest.raises(ValueError):
            compute_quote_prices(mid=-0.1, target_half_spread_pct=0.5, max_spread_pct=3.5)

    def test_invalid_spreads_raise(self) -> None:
        with pytest.raises(ValueError):
            compute_quote_prices(mid=0.5, target_half_spread_pct=0.0, max_spread_pct=3.5)
        with pytest.raises(ValueError):
            compute_quote_prices(mid=0.5, target_half_spread_pct=0.5, max_spread_pct=0.0)


# ── size_from_capital ─────────────────────────────────────────────────────────

class TestSizeFromCapital:
    def test_basic(self) -> None:
        """$20 at $0.50 → 40 shares, clamped to min_shares=20 still passes."""
        assert size_from_capital(price=0.50, capital_usd=20.0, min_shares=20) == 40.0

    def test_below_min_returns_zero(self) -> None:
        """$5 at $0.50 = 10 shares, below min 20 → skip market."""
        assert size_from_capital(price=0.50, capital_usd=5.0, min_shares=20) == 0.0

    def test_exactly_at_min(self) -> None:
        """$10 at $0.50 = 20 shares, exactly min → accept."""
        assert size_from_capital(price=0.50, capital_usd=10.0, min_shares=20) == 20.0

    def test_max_clamp(self) -> None:
        """$10,000 at $0.50 = 20000 shares, capped at max=100."""
        assert size_from_capital(
            price=0.50, capital_usd=10_000.0, min_shares=20, max_shares=100
        ) == 100.0

    def test_rounding_down_never_overallocates(self) -> None:
        """$10.33 at $0.50 = 20.66 → round DOWN to 20.66 ≥ min, keep 20.66.
        Critical: we must NEVER round up because that would use more
        capital than we have."""
        result = size_from_capital(price=0.50, capital_usd=10.33, min_shares=20)
        # 10.33/0.50 = 20.66; floor to 0.01 = 20.66
        assert result == 20.66
        # Cost cannot exceed input capital
        assert result * 0.50 <= 10.33 + 1e-9

    def test_longshot_price_large_size(self) -> None:
        """$5 at $0.03 = 166.67 shares — much larger denomination due to low price."""
        result = size_from_capital(price=0.03, capital_usd=5.0, min_shares=20)
        assert result == 166.66   # floor to 0.01

    def test_zero_capital(self) -> None:
        assert size_from_capital(price=0.5, capital_usd=0, min_shares=20) == 0.0

    def test_zero_price(self) -> None:
        assert size_from_capital(price=0.0, capital_usd=10.0, min_shares=20) == 0.0

    def test_zero_min_shares(self) -> None:
        """Defensive: a misconfigured market with min=0 returns 0 (skip)."""
        assert size_from_capital(price=0.5, capital_usd=10.0, min_shares=0) == 0.0


# ── needs_reprice ─────────────────────────────────────────────────────────────

class TestNeedsReprice:
    def test_no_drift(self) -> None:
        assert needs_reprice(existing_price=0.5, current_mid=0.5, reprice_threshold_pct=0.5) is False

    def test_small_drift_below_threshold(self) -> None:
        """0.5 → 0.501 is 0.2% drift, below 0.5% threshold → keep quote."""
        assert needs_reprice(0.5, 0.501, 0.5) is False

    def test_drift_exactly_at_threshold(self) -> None:
        """At threshold, NOT triggered (only strictly greater)."""
        # 0.5 → 0.5025 is exactly 0.5% drift
        assert needs_reprice(0.5, 0.5025, 0.5) is False

    def test_drift_above_threshold(self) -> None:
        """0.5 → 0.51 is 2% drift, above 0.5% threshold → reprice."""
        assert needs_reprice(0.5, 0.51, 0.5) is True

    def test_negative_drift_same_behavior(self) -> None:
        """drift 'below' mid triggers just like drift above."""
        assert needs_reprice(0.51, 0.5, 0.5) is True

    def test_zero_mid_safe_default(self) -> None:
        """Defensive: never emit reprice signal on invalid mid."""
        assert needs_reprice(0.5, 0.0, 0.5) is False

    def test_zero_threshold_safe_default(self) -> None:
        """reprice_threshold_pct=0 means never reprice; defensive no-op."""
        assert needs_reprice(0.5, 0.6, 0.0) is False


# ── quote_is_within_rewards_envelope ──────────────────────────────────────────

class TestEnvelopeCheck:
    def test_inside_envelope(self) -> None:
        assert quote_is_within_rewards_envelope(price=0.485, mid=0.5, max_spread_pct=3.5) is True

    def test_outside_envelope(self) -> None:
        """Price 0.45 is 10% from mid=0.5, outside 3.5% envelope."""
        assert quote_is_within_rewards_envelope(price=0.45, mid=0.5, max_spread_pct=3.5) is False

    def test_exactly_at_boundary(self) -> None:
        """At boundary → inclusive (still qualifies)."""
        # 3.5% of 0.5 = 0.0175, so price 0.4825 is exactly at boundary
        assert quote_is_within_rewards_envelope(price=0.4825, mid=0.5, max_spread_pct=3.5) is True

    def test_invalid_mid(self) -> None:
        assert quote_is_within_rewards_envelope(price=0.5, mid=0, max_spread_pct=3.5) is False


# ── Integration: compute_quote_prices output always passes envelope check ────

def test_compute_prices_always_within_envelope() -> None:
    """Regression guard: our computed prices should never fall outside the
    rewards envelope we fed in. Round-trip invariant."""
    for mid in (0.1, 0.3, 0.5, 0.7, 0.9):
        for half in (0.3, 0.5, 1.0, 2.0):
            for maxs in (1.5, 3.0, 3.5):
                bid, ask = compute_quote_prices(mid, half, maxs)
                # Both prices must fall within max envelope
                assert quote_is_within_rewards_envelope(bid, mid, maxs), (
                    f"bid={bid} outside envelope for mid={mid} max={maxs}"
                )
                assert quote_is_within_rewards_envelope(ask, mid, maxs), (
                    f"ask={ask} outside envelope for mid={mid} max={maxs}"
                )
                # And sanity: bid < mid < ask
                assert bid <= mid <= ask or math.isclose(bid, mid, abs_tol=0.001)
