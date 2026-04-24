"""T-58: unit tests for trading/maker_ledger.py

Pure-logic tests with AsyncMock'd asyncpg pool. Verify SQL shape + params
+ return values. No real DB needed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from trading.maker_ledger import (
    cancel_quote,
    fetch_live_quotes,
    fill_total_for_quote,
    insert_quote,
    inventory_for_market,
    mark_matched,
    mark_rejected,
    record_fill,
    update_clob_order_id,
)


def _pool_with(fetchrow=None, fetch=None) -> AsyncMock:
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=fetchrow) if fetchrow is not None else AsyncMock()
    pool.fetch = AsyncMock(return_value=fetch) if fetch is not None else AsyncMock()
    pool.execute = AsyncMock()
    return pool


# ── insert_quote ──────────────────────────────────────────────────────────────

def test_insert_quote_returns_id() -> None:
    pool = _pool_with(fetchrow={"id": 42})
    result = asyncio.run(insert_quote(
        pool, market_id="mid-1", side="BUY", token_id="tok",
        price=0.485, size_shares=20.0, slug="test-slug",
    ))
    assert result == 42
    # Verify SQL was called with expected param order
    pool.fetchrow.assert_awaited_once()
    call_args = pool.fetchrow.await_args.args
    assert "INSERT INTO maker_quotes" in call_args[0]
    assert call_args[1] == "mid-1"
    assert call_args[2] == "test-slug"
    assert call_args[3] == "BUY"
    assert call_args[4] == "tok"
    assert call_args[5] == 0.485
    assert call_args[6] == 20.0
    assert call_args[7] is None   # clob_order_id default
    assert call_args[8] == "LIVE"


def test_insert_quote_non_live_status_accepted() -> None:
    """Agent may insert with status other than LIVE (rare — e.g., for
    post-hoc reconstruction). Make sure it's passed through."""
    pool = _pool_with(fetchrow={"id": 7})
    asyncio.run(insert_quote(
        pool, market_id="mid-1", side="SELL", token_id="tok",
        price=0.52, size_shares=30.0, status="REJECTED",
    ))
    assert pool.fetchrow.await_args.args[8] == "REJECTED"


# ── cancel_quote — idempotent ─────────────────────────────────────────────────

def test_cancel_quote_only_touches_live() -> None:
    """SQL WHERE status = 'LIVE' guards against clobbering MATCHED rows
    when a race fires a stale cancel."""
    pool = _pool_with()
    asyncio.run(cancel_quote(pool, 123))
    sql = pool.execute.await_args.args[0]
    assert "status = 'CANCELLED'" in sql
    assert "status = 'LIVE'" in sql    # the guard
    assert pool.execute.await_args.args[1] == 123


# ── mark_matched ──────────────────────────────────────────────────────────────

def test_mark_matched() -> None:
    pool = _pool_with()
    asyncio.run(mark_matched(pool, 99))
    sql = pool.execute.await_args.args[0]
    assert "status = 'MATCHED'" in sql
    assert pool.execute.await_args.args[1] == 99


# ── mark_rejected ─────────────────────────────────────────────────────────────

def test_mark_rejected_stamps_cancelled_at() -> None:
    """REJECTED also stamps cancelled_at — unlike LIVE→CANCELLED which needs
    the guard, REJECTED is terminal with no race possibility."""
    pool = _pool_with()
    asyncio.run(mark_rejected(pool, 55))
    sql = pool.execute.await_args.args[0]
    assert "status = 'REJECTED'" in sql
    assert "cancelled_at = NOW()" in sql
    assert pool.execute.await_args.args[1] == 55


# ── fetch_live_quotes ─────────────────────────────────────────────────────────

def test_fetch_live_quotes_no_filter() -> None:
    pool = _pool_with(fetch=[])
    asyncio.run(fetch_live_quotes(pool))
    sql = pool.fetch.await_args.args[0]
    assert "status = 'LIVE'" in sql
    assert "market_id = $1" not in sql    # no market filter
    # No params beyond SQL
    assert len(pool.fetch.await_args.args) == 1


def test_fetch_live_quotes_with_market_filter() -> None:
    pool = _pool_with(fetch=[])
    asyncio.run(fetch_live_quotes(pool, market_id="mid-42"))
    sql = pool.fetch.await_args.args[0]
    assert "status = 'LIVE'" in sql
    assert "market_id = $1" in sql
    assert pool.fetch.await_args.args[1] == "mid-42"


def test_fetch_live_quotes_parses_rows() -> None:
    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc)
    pool = _pool_with(fetch=[{
        "id": 1, "market_id": "m", "slug": "s", "side": "BUY",
        "token_id": "tok", "price": 0.5, "size_shares": 20.0,
        "clob_order_id": "abc", "placed_at": now,
        "cancelled_at": None, "status": "LIVE",
    }])
    quotes = asyncio.run(fetch_live_quotes(pool))
    assert len(quotes) == 1
    q = quotes[0]
    assert q.id == 1
    assert q.market_id == "m"
    assert q.side == "BUY"
    assert q.clob_order_id == "abc"
    assert q.status == "LIVE"


# ── record_fill ───────────────────────────────────────────────────────────────

def test_record_fill_returns_id() -> None:
    pool = _pool_with(fetchrow={"id": 7})
    fid = asyncio.run(record_fill(
        pool, quote_id=42, market_id="m", side="BUY",
        fill_price=0.485, fill_size=20.0, clob_trade_id="trade-x",
    ))
    assert fid == 7
    call = pool.fetchrow.await_args.args
    assert "INSERT INTO maker_fills" in call[0]
    assert call[1] == 42    # quote_id
    assert call[2] == "m"
    assert call[3] == "BUY"
    assert call[4] == 0.485
    assert call[5] == 20.0
    assert call[6] == "trade-x"


# ── fill_total_for_quote ──────────────────────────────────────────────────────

def test_fill_total_for_quote_sums_correctly() -> None:
    pool = _pool_with(fetchrow={"total": 15.5})
    assert asyncio.run(fill_total_for_quote(pool, 42)) == 15.5


def test_fill_total_handles_null() -> None:
    """COALESCE should give 0 but mock returns None — verify Python
    conversion yields 0.0 not TypeError."""
    pool = _pool_with(fetchrow={"total": None})
    assert asyncio.run(fill_total_for_quote(pool, 42)) == 0.0


# ── inventory_for_market ──────────────────────────────────────────────────────

def test_inventory_net_long_from_buys() -> None:
    """10 bought, 3 sold → net +7 long."""
    pool = _pool_with(fetchrow={"buys": 10.0, "sells": 3.0})
    inv = asyncio.run(inventory_for_market(pool, "m", "tok"))
    assert inv == 7.0


def test_inventory_net_short() -> None:
    pool = _pool_with(fetchrow={"buys": 5.0, "sells": 12.0})
    inv = asyncio.run(inventory_for_market(pool, "m", "tok"))
    assert inv == -7.0


def test_inventory_zero_when_no_fills() -> None:
    pool = _pool_with(fetchrow={"buys": None, "sells": None})
    assert asyncio.run(inventory_for_market(pool, "m", "tok")) == 0.0


def test_inventory_sql_joins_fills_to_quotes() -> None:
    """Inventory must filter by (market_id, token_id) — token_id lives on
    maker_quotes, fill_size on maker_fills, so the JOIN is critical."""
    pool = _pool_with(fetchrow={"buys": 0, "sells": 0})
    asyncio.run(inventory_for_market(pool, "m", "tok"))
    sql = pool.fetchrow.await_args.args[0]
    assert "JOIN maker_quotes" in sql
    assert "ON mf.quote_id = mq.id" in sql
    assert "mf.market_id = $1" in sql
    assert "mq.token_id = $2" in sql


# ── update_clob_order_id ──────────────────────────────────────────────────────

def test_update_clob_order_id() -> None:
    pool = _pool_with()
    asyncio.run(update_clob_order_id(pool, 42, "clob-abc-123"))
    call = pool.execute.await_args.args
    assert "UPDATE maker_quotes SET clob_order_id" in call[0]
    assert call[1] == "clob-abc-123"
    assert call[2] == 42
