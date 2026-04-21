"""
Unit tests for trading/risk_guard.py

Pure-logic tests only — DB and CLOB calls are mocked via simple stubs.

Run with:
    pytest tests/test_risk_guard.py -v
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from trading.risk_guard import (
    _GAME_WINDOW_HOURS,
    ask_looks_orphan,
    bid_looks_orphan,
    circuit_breaker_check,
    correlation_check,
)


# ── T-48: bid_looks_orphan sanity guard ──────────────────────────────────────
#
# Observed real cases on paper-trading Mac Mini:
#  - Atlanta Braves YES entry 0.515, CLOB bid=0.01, ask unknown → false stop-loss
#  - Seattle Mariners NO entry 0.555, same pattern
#  - Charlotte Hornets YES entry 0.485, same pattern
# Without this guard, all three fired -95% stop-losses within 5 min of opening.


class TestBidLooksOrphan:
    def test_healthy_bid_near_entry_is_not_orphan(self):
        """bid within normal range of entry → not orphan."""
        assert bid_looks_orphan(bid=0.48, ask=0.52, entry_price=0.50) is False

    def test_mild_drop_not_orphan(self):
        """bid down 20% with tight spread → real price move, not dust."""
        assert bid_looks_orphan(bid=0.40, ask=0.42, entry_price=0.50) is False

    def test_real_crash_fires_stop_loss(self):
        """Both sides collapsed — bid and ask both at 0.05 with entry 0.50.
        This is a legitimate market crash; stop-loss SHOULD fire. Guard
        must return False (NOT orphan)."""
        assert bid_looks_orphan(bid=0.05, ask=0.08, entry_price=0.50) is False

    def test_dust_bid_on_thin_book_is_orphan(self):
        """bid=0.01 with ask=0.50 (real market-maker quote) — this is the
        Mac Mini 2026-04-17 case. Bid is an orphan dust order, NOT a real
        price collapse. Guard must return True to suppress stop-loss."""
        assert bid_looks_orphan(bid=0.01, ask=0.50, entry_price=0.515) is True

    def test_dust_bid_wide_spread_realistic_entry(self):
        """bid=0.02 ask=0.48 entry=0.48 — ask/bid ratio 24, classic thin book."""
        assert bid_looks_orphan(bid=0.02, ask=0.48, entry_price=0.48) is True

    def test_bid_far_below_no_ask_is_orphan(self):
        """If there's no ask at all AND bid is far below entry, we cannot
        confirm a real crash → assume orphan."""
        assert bid_looks_orphan(bid=0.05, ask=0.0, entry_price=0.50) is True

    def test_longshot_position_no_guard(self):
        """Entry < 0.10 = longshot. 0.01 collapse might be real ('dog lost');
        we let the stop-loss fire. Guard returns False."""
        assert bid_looks_orphan(bid=0.01, ask=0.05, entry_price=0.08) is False

    def test_zero_bid_not_orphan_by_definition(self):
        """bid<=0 is handled by an earlier guard — this function should
        return False so it doesn't double-block (let the zero-bid check
        upstream handle it)."""
        assert bid_looks_orphan(bid=0.0, ask=0.5, entry_price=0.50) is False
        assert bid_looks_orphan(bid=-0.01, ask=0.5, entry_price=0.50) is False

    def test_exact_boundary_bid_floor(self):
        """bid exactly at _BID_FLOOR_RATIO (30%) of entry → NOT orphan (at or above
        floor). Just above should also be not-orphan. Just below is candidate."""
        # At 30% exactly
        assert bid_looks_orphan(bid=0.15, ask=0.50, entry_price=0.50) is False
        # Just above
        assert bid_looks_orphan(bid=0.16, ask=0.50, entry_price=0.50) is False
        # Just below + wide spread → orphan
        assert bid_looks_orphan(bid=0.10, ask=0.50, entry_price=0.50) is True


# ── T-52: ask_looks_orphan — symmetric entry-side guard ──────────────────────
#
# Post-mortem of pitcher-signal losses (positions 18-25) showed entry at
# signal_price 0.485-0.615 on dead books (bid=0.01 ask=0.99). Had those
# been live trades instead of dry_run paper, we would have bought at 0.99
# for something worth ~0.50 — an instant 98% paper loss. This guard blocks
# entry into those books at the source, not just exits from them.


class TestAskLooksOrphan:
    def test_healthy_ask_near_signal_is_not_orphan(self):
        """ask within normal range of signal → not orphan."""
        assert ask_looks_orphan(bid=0.48, ask=0.52, signal_price=0.50) is False

    def test_mild_overprice_not_orphan(self):
        """ask 10% above signal with tight spread → real overpricing, not dust."""
        assert ask_looks_orphan(bid=0.52, ask=0.55, signal_price=0.50) is False

    def test_dust_ask_on_thin_book_is_orphan(self):
        """The 2026-04-21 pitcher-signal case: bid=0.01 ask=0.99 signal=0.50.
        Classic dead book; entry must be refused. Guard returns True."""
        assert ask_looks_orphan(bid=0.01, ask=0.99, signal_price=0.50) is True

    def test_dead_book_on_favorite_is_orphan(self):
        """Favorite signal 0.85 with bid=0.01 ask=0.99 — still a dead book;
        1-ask=0.01 is tiny relative to 1-signal=0.15."""
        assert ask_looks_orphan(bid=0.01, ask=0.99, signal_price=0.85) is True

    def test_near_certain_favorite_tight_book_not_orphan(self):
        """Favorite signal 0.92 with tight real book bid=0.90 ask=0.94 —
        ask_comp=0.06 vs sig_comp=0.08, ratio 0.75, not orphan."""
        assert ask_looks_orphan(bid=0.90, ask=0.94, signal_price=0.92) is False

    def test_dust_ask_no_bid_is_orphan(self):
        """ask 0.99 and no bid at all — clearly no market."""
        assert ask_looks_orphan(bid=0.0, ask=0.99, signal_price=0.50) is True

    def test_longshot_signal_no_guard(self):
        """Signal < 0.10 is a longshot — wide ask is normal, don't guard."""
        assert ask_looks_orphan(bid=0.01, ask=0.99, signal_price=0.08) is False

    def test_degenerate_ask_zero(self):
        """ask=0 means no ask on book — handled by an upstream check, not ours."""
        assert ask_looks_orphan(bid=0.40, ask=0.0, signal_price=0.50) is False

    def test_degenerate_ask_one(self):
        """ask=1.0 means CLOB shows no seller at any price — treat as degenerate."""
        assert ask_looks_orphan(bid=0.40, ask=1.0, signal_price=0.50) is False

    def test_mirror_of_bid_case_symmetric(self):
        """Sanity: for signal=0.50, the orphan condition on ask should mirror
        the bid case — complement-of-0.99 is 0.01, same as dust bid at 0.01."""
        assert ask_looks_orphan(bid=0.01, ask=0.99, signal_price=0.50) is True
        assert bid_looks_orphan(bid=0.01, ask=0.99, entry_price=0.50) is True


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def config():
    return {
        "trading": {
            "stop_loss_pct": 40.0,
            "stop_loss_check_sec": 300,
            "daily_loss_limit_usd": 20.0,
            "max_positions_per_game": 2,
            "max_positions_per_sport": 4,
        }
    }


def _make_pool(today_pnl=0.0, game_count=0, sport_count=0, sport="basketball"):
    """Return a mock asyncpg pool with pre-configured query results."""
    pool = AsyncMock()

    async def fetchrow(_sql, *args):
        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "today_pnl": today_pnl,
            "sport": sport,
        }.get(k)
        return row

    async def fetchval(sql, *args):
        # game_count query comes first, sport_count second
        if "game_start" in sql or "ABS" in sql:
            return game_count
        return sport_count

    pool.fetchrow = fetchrow
    pool.fetchval = fetchval
    return pool


# ── circuit_breaker_check ─────────────────────────────────────────────────────

class TestCircuitBreakerCheck:
    def test_no_losses_not_blocked(self, config):
        pool = _make_pool(today_pnl=0.0)
        blocked, reason = asyncio.run(circuit_breaker_check(pool, config))
        assert blocked is False
        assert reason == ""

    def test_profit_not_blocked(self, config):
        pool = _make_pool(today_pnl=5.0)
        blocked, _ = asyncio.run(circuit_breaker_check(pool, config))
        assert blocked is False

    def test_small_loss_not_blocked(self, config):
        pool = _make_pool(today_pnl=-10.0)
        blocked, _ = asyncio.run(circuit_breaker_check(pool, config))
        assert blocked is False

    def test_exactly_at_limit_blocked(self, config):
        """Loss exactly at daily_loss_limit_usd → blocked."""
        pool = _make_pool(today_pnl=-20.0)
        blocked, reason = asyncio.run(circuit_breaker_check(pool, config))
        assert blocked is True
        assert "circuit breaker" in reason
        assert "$20" in reason

    def test_over_limit_blocked(self, config):
        pool = _make_pool(today_pnl=-35.0)
        blocked, reason = asyncio.run(circuit_breaker_check(pool, config))
        assert blocked is True
        assert "−$20" in reason

    def test_custom_limit(self, config):
        config["trading"]["daily_loss_limit_usd"] = 50.0
        pool = _make_pool(today_pnl=-49.0)
        blocked, _ = asyncio.run(circuit_breaker_check(pool, config))
        assert blocked is False

        pool2 = _make_pool(today_pnl=-51.0)
        blocked2, _ = asyncio.run(circuit_breaker_check(pool2, config))
        assert blocked2 is True


# ── correlation_check ─────────────────────────────────────────────────────────

class TestCorrelationCheck:
    def _game_start(self, hours_offset=6.0):
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        return now + timedelta(hours=hours_offset)

    def test_no_existing_positions_allowed(self, config):
        pool = _make_pool(game_count=0, sport_count=0)
        blocked, _ = asyncio.run(
            correlation_check(pool, config, self._game_start(), "market-1")
        )
        assert blocked is False

    def test_one_existing_game_position_allowed(self, config):
        """1 position on same game, max=2 → still allowed."""
        pool = _make_pool(game_count=1, sport_count=1)
        blocked, _ = asyncio.run(
            correlation_check(pool, config, self._game_start(), "market-1")
        )
        assert blocked is False

    def test_at_game_limit_blocked(self, config):
        """2 positions on same game, max=2 → blocked."""
        pool = _make_pool(game_count=2, sport_count=2)
        blocked, reason = asyncio.run(
            correlation_check(pool, config, self._game_start(), "market-1")
        )
        assert blocked is True
        assert "game" in reason
        assert "2" in reason

    def test_at_sport_limit_blocked(self, config):
        """4 positions in same sport, max=4 → blocked."""
        pool = _make_pool(game_count=0, sport_count=4)
        blocked, reason = asyncio.run(
            correlation_check(pool, config, self._game_start(), "market-1")
        )
        assert blocked is True
        assert "sport" in reason.lower() or "basketball" in reason

    def test_no_game_start_skips_game_check(self, config):
        """game_start=None → game window check skipped, sport check still runs."""
        pool = _make_pool(game_count=99, sport_count=0)
        blocked, _ = asyncio.run(
            correlation_check(pool, config, None, "market-1")
        )
        # game_count is irrelevant when game_start is None
        assert blocked is False

    def test_custom_limits_respected(self, config):
        config["trading"]["max_positions_per_game"] = 1
        pool = _make_pool(game_count=1, sport_count=0)
        blocked, reason = asyncio.run(
            correlation_check(pool, config, self._game_start(), "market-1")
        )
        assert blocked is True

    def test_game_window_constant_is_positive(self):
        assert _GAME_WINDOW_HOURS > 0


# ── stop_loss threshold arithmetic ───────────────────────────────────────────
# The monitor itself requires CLOB + DB, so we only test the math here.

class TestStopLossThreshold:
    @pytest.mark.parametrize("entry,stop_pct,bid,should_trigger", [
        (0.200, 40.0, 0.119, True),   # bid 40.5% below entry → trigger
        (0.200, 40.0, 0.120, False),  # bid exactly at threshold (0.2*0.6=0.12) → no trigger
        (0.200, 40.0, 0.150, False),  # bid 25% below → no trigger
        (0.500, 40.0, 0.290, True),   # different price scale
        (0.100, 25.0, 0.074, True),   # 25% stop-loss
        (0.100, 25.0, 0.076, False),  # above 25% threshold → no trigger
    ])
    def test_threshold(self, entry, stop_pct, bid, should_trigger):
        threshold = 1.0 - stop_pct / 100.0
        stop_price = entry * threshold
        triggered = bid < stop_price
        assert triggered is should_trigger, (
            f"entry={entry}, stop_pct={stop_pct}, bid={bid}, "
            f"stop_price={stop_price:.4f}, triggered={triggered}"
        )


# ── take_profit threshold arithmetic ─────────────────────────────────────────

class TestTakeProfitThreshold:
    @pytest.mark.parametrize("entry,tp_pct,bid,should_trigger", [
        (0.200, 40.0, 0.281, True),   # bid 40.5% above entry → trigger
        (0.200, 40.0, 0.279, False),  # bid just below threshold (0.2*1.4≈0.28) → no trigger
        (0.200, 40.0, 0.250, False),  # bid 25% above → no trigger
        (0.500, 40.0, 0.701, True),   # different price scale
        (0.100, 25.0, 0.126, True),   # 25% take-profit
        (0.100, 25.0, 0.124, False),  # below 25% threshold → no trigger
        (0.300, 50.0, 0.451, True),   # 50% take-profit
        (0.300, 50.0, 0.449, False),  # just under 50% → no trigger
    ])
    def test_threshold(self, entry, tp_pct, bid, should_trigger):
        tp_price = entry * (1.0 + tp_pct / 100.0)
        triggered = bid >= tp_price
        assert triggered is should_trigger, (
            f"entry={entry}, tp_pct={tp_pct}, bid={bid}, "
            f"tp_price={tp_price:.4f}, triggered={triggered}"
        )

    def test_disabled_when_zero(self):
        """take_profit_pct=0 means tp_threshold is None — never triggers."""
        tp_pct = 0.0
        tp_threshold = (1.0 + tp_pct / 100.0) if tp_pct > 0 else None
        assert tp_threshold is None

    def test_enabled_when_positive(self):
        tp_pct = 40.0
        tp_threshold = (1.0 + tp_pct / 100.0) if tp_pct > 0 else None
        assert tp_threshold == 1.40


# ── order timeout ─────────────────────────────────────────────────────────────

class TestOrderTimeout:
    def test_timeout_triggers_at_limit(self):
        """Order exactly at max_pending_hours should be cancelled."""
        max_h = 6.0
        order_age_h = 6.0
        timed_out = order_age_h >= max_h
        assert timed_out is True

    def test_timeout_does_not_trigger_before_limit(self):
        max_h = 6.0
        order_age_h = 5.9
        timed_out = order_age_h >= max_h
        assert timed_out is False

    def test_timeout_triggers_well_over_limit(self):
        max_h = 6.0
        order_age_h = 12.0
        timed_out = order_age_h >= max_h
        assert timed_out is True

    def test_reprice_skipped_when_timed_out(self):
        """If age >= max_pending, re-pricing branch is never reached."""
        max_h = 6.0
        reprice_after_h = 2.0
        order_age_h = 7.0
        # Timeout check runs first; re-price only if age < max and age >= reprice_after
        would_timeout = order_age_h >= max_h
        would_reprice = not would_timeout and order_age_h >= reprice_after_h
        assert would_timeout is True
        assert would_reprice is False
