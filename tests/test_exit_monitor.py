"""T-49: stagnation exit must apply the same orphan-dust-quote guard that
risk_guard uses (T-48). Without the guard, stagnation exits closed at
bid=0.01 on thin pre-game books and booked -95% fake losses.

Mirrors the pattern of TestBidLooksOrphan in test_risk_guard.py but exercises
the integration in check_stagnation_exit.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from trading.exit_monitor import check_stagnation_exit


def _flat_snapshots(n: int, mid: float = 0.5):
    """Return n identical snapshots so moves=[0, 0, ...] → stagnation fires."""
    return [{"mid_price": mid} for _ in range(n)]


def _pos_row(entry_price: float = 0.5, market_id: str = "mid-1") -> dict:
    now = datetime.now(tz=timezone.utc)
    return {
        "id": 42,
        "market_id": market_id,
        "slug": "mlb-test",
        "token_id": "0xTOK",
        "size_shares": 10.0,
        "entry_price": entry_price,
        "entry_ts": now - timedelta(hours=1),     # >30min ago — passes held guard
        "game_start": now + timedelta(hours=5),    # >2h out — passes game guard
    }


def test_stagnation_skipped_when_bid_looks_orphan() -> None:
    """Core T-49 regression: with entry=0.5 and live quote bid=0.01 ask=0.99,
    stagnation must NOT close the position. close_position must not be called."""
    pool = AsyncMock()

    async def fake_fetch(sql, *args):
        # First call: positions query (returns 1 stagnant pos).
        # Second call: price_snapshots query (returns flat prices).
        if "open_positions" in sql:
            return [_pos_row(entry_price=0.5)]
        if "price_snapshots" in sql:
            return _flat_snapshots(3, mid=0.5)
        return []

    pool.fetch = fake_fetch

    executor = AsyncMock()
    executor.get_market_info = AsyncMock(return_value={"bid": 0.01, "ask": 0.99})
    executor.sell = AsyncMock()   # must NOT be called when guard fires

    with patch("trading.exit_monitor.close_position", new_callable=AsyncMock) as mock_close, \
         patch("trading.exit_monitor.log_order", new_callable=AsyncMock), \
         patch("trading.exit_monitor.send_exit_notification", new_callable=AsyncMock):
        asyncio.run(check_stagnation_exit(
            pool, executor, config={"trading": {}},
            tg_token="tok", tg_chat_id="chat",
        ))

    executor.sell.assert_not_awaited()
    mock_close.assert_not_awaited()


def test_stagnation_proceeds_when_quote_is_healthy() -> None:
    """Real-market stagnation (tight bid/ask, no dust) → stagnation exit
    proceeds normally. bid=0.48 ask=0.52 around entry=0.5 is a trustworthy
    quote — not orphan — so close_position IS called at bid."""
    pool = AsyncMock()

    async def fake_fetch(sql, *args):
        if "open_positions" in sql:
            return [_pos_row(entry_price=0.5)]
        if "price_snapshots" in sql:
            return _flat_snapshots(3, mid=0.5)
        return []

    pool.fetch = fake_fetch

    executor = AsyncMock()
    executor.get_market_info = AsyncMock(return_value={"bid": 0.48, "ask": 0.52})
    executor.sell = AsyncMock(return_value={"order_id": "DRY_SELL_abc", "status": "dry_run"})

    with patch("trading.exit_monitor.close_position", new_callable=AsyncMock) as mock_close, \
         patch("trading.exit_monitor.log_order", new_callable=AsyncMock), \
         patch("trading.exit_monitor.send_exit_notification", new_callable=AsyncMock):
        mock_close.return_value = -0.20
        asyncio.run(check_stagnation_exit(
            pool, executor, config={"trading": {}},
            tg_token="tok", tg_chat_id="chat",
        ))

    executor.sell.assert_awaited_once()
    mock_close.assert_awaited_once()
    # close_position(pool, position_id, exit_price=best_bid)
    close_args = mock_close.await_args.args
    assert close_args[1] == 42          # position_id
    assert abs(close_args[2] - 0.48) < 1e-9   # exit at bid, not dust


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
