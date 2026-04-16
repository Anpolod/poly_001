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
    circuit_breaker_check,
    correlation_check,
)


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
