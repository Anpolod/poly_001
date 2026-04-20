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


# ── T-51: auto-exit-before-game must also guard against dust bid ──────────────
# Unlike T-49 (stagnation) which CAN skip the exit, T-51 can't — game is about
# to start. So instead of skipping, auto-exit falls back to entry_price when
# bid looks orphan, recording a wash trade rather than a fake -95% loss.


from trading.exit_monitor import check_and_exit


def test_auto_exit_uses_entry_price_fallback_on_orphan_bid() -> None:
    """Position at game time, orderbook still thin → close at entry_price
    not dust bid. Records wash trade (pnl near 0) instead of phantom -95%."""
    from unittest.mock import patch, AsyncMock
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)
    pool = AsyncMock()
    pool.fetch = AsyncMock()   # not used; get_positions_near_expiry is patched

    pos = {
        "id": 42,
        "slug": "mlb-test",
        "market_id": "mid-1",
        "token_id": "0xTOK",
        "size_shares": 10.0,
        "entry_price": 0.55,
        "game_start": now + timedelta(minutes=20),   # within exit_minutes_before
    }

    executor = AsyncMock()
    executor.get_market_info = AsyncMock(return_value={"bid": 0.01, "ask": 0.99})
    executor.sell = AsyncMock(return_value={"order_id": "DRY_SELL_abc", "status": "dry_run"})

    with patch("trading.exit_monitor.get_positions_near_expiry",
               new=AsyncMock(return_value=[pos])), \
         patch("trading.exit_monitor.close_position",
               new_callable=AsyncMock) as mock_close, \
         patch("trading.exit_monitor.log_order", new_callable=AsyncMock), \
         patch("trading.exit_monitor.send_exit_notification", new_callable=AsyncMock):
        mock_close.return_value = 0.0
        asyncio.run(check_and_exit(pool, executor, config={"trading": {}},
                                   tg_token="tok", tg_chat_id="chat"))

    # Sell was placed (auto-exit MUST close — game starts soon)
    executor.sell.assert_awaited_once()
    sell_args = executor.sell.await_args.args
    assert sell_args[0] == "0xTOK"
    # Critical: exit price is entry_price (0.55), NOT dust bid (0.01)
    assert abs(sell_args[1] - 0.55) < 1e-9, (
        f"auto-exit used dust bid instead of entry_price fallback: "
        f"sold at {sell_args[1]}"
    )
    # close_position called with same entry_price as exit_price
    close_args = mock_close.await_args.args
    assert close_args[1] == 42
    assert abs(close_args[2] - 0.55) < 1e-9


def test_auto_exit_uses_live_bid_when_quote_is_healthy() -> None:
    """Real market with tight spread → close at live bid as normal."""
    from unittest.mock import patch, AsyncMock
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)
    pos = {
        "id": 43, "slug": "mlb-test2", "market_id": "mid-2",
        "token_id": "0xTOK2", "size_shares": 8.0, "entry_price": 0.50,
        "game_start": now + timedelta(minutes=25),
    }

    executor = AsyncMock()
    executor.get_market_info = AsyncMock(return_value={"bid": 0.48, "ask": 0.52})
    executor.sell = AsyncMock(return_value={"order_id": "DRY_SELL_def", "status": "dry_run"})

    with patch("trading.exit_monitor.get_positions_near_expiry",
               new=AsyncMock(return_value=[pos])), \
         patch("trading.exit_monitor.close_position",
               new_callable=AsyncMock) as mock_close, \
         patch("trading.exit_monitor.log_order", new_callable=AsyncMock), \
         patch("trading.exit_monitor.send_exit_notification", new_callable=AsyncMock):
        mock_close.return_value = -0.16
        asyncio.run(check_and_exit(AsyncMock(), executor, config={"trading": {}},
                                   tg_token="tok", tg_chat_id="chat"))

    executor.sell.assert_awaited_once()
    sell_args = executor.sell.await_args.args
    assert abs(sell_args[1] - 0.48) < 1e-9   # sold at real bid, not entry


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
