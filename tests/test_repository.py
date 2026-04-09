"""Tests for db/repository.py — mocks asyncpg pool, no real DB required"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.repository import Repository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo() -> tuple[Repository, MagicMock]:
    """Return (repo, mock_pool) with all pool methods as AsyncMocks."""
    pool = MagicMock()
    pool.execute = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchval = AsyncMock(return_value=None)
    pool.close = AsyncMock(return_value=None)
    return Repository(pool), pool


def _utc(**kwargs) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kwargs)


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------

class TestUpsertMarket:
    async def test_calls_execute_with_correct_params(self):
        repo, pool = _make_repo()
        event_start = _utc(hours=24)
        market = {
            "id": "mkt-1",
            "slug": "nba-det-phi",
            "question": "Will Detroit win?",
            "sport": "basketball",
            "league": "nba",
            "event_start": event_start,
            "token_id_yes": "tok_yes",
            "token_id_no": "tok_no",
            "status": "active",
            "fee_rate_yes": 0.0075,
            "fee_rate_no": 0.0075,
        }
        await repo.upsert_market(market)

        pool.execute.assert_called_once()
        args = pool.execute.call_args[0]
        # positional params: $1=id, $2=slug, $3=question, $4=sport, $5=league,
        # $6=event_start, $7=token_id_yes, $8=token_id_no, $9=status,
        # $10=fee_rate_yes, $11=fee_rate_no
        assert args[1] == "mkt-1"
        assert args[2] == "nba-det-phi"
        assert args[4] == "basketball"
        assert args[6] == event_start
        assert args[7] == "tok_yes"
        assert args[8] == "tok_no"
        assert args[9] == "active"

    async def test_uses_default_status_active(self):
        repo, pool = _make_repo()
        market = {
            "id": "mkt-2", "slug": "sl", "question": "q",
            "sport": "basketball", "league": "nba",
            "event_start": _utc(hours=10),
            "token_id_yes": "t1", "token_id_no": "t2",
            # no "status" key
        }
        await repo.upsert_market(market)
        args = pool.execute.call_args[0]
        assert args[9] == "active"


class TestGetActiveMarkets:
    async def test_returns_pool_fetch_result(self):
        repo, pool = _make_repo()
        fake_rows = [{"id": "m1"}, {"id": "m2"}]
        pool.fetch.return_value = fake_rows

        result = await repo.get_active_markets()

        pool.fetch.assert_called_once()
        assert result == fake_rows

    async def test_sql_filters_active_and_future(self):
        repo, pool = _make_repo()
        await repo.get_active_markets()

        sql = pool.fetch.call_args[0][0]
        assert "status = 'active'" in sql
        assert "event_start > NOW()" in sql


class TestUpdateMarketStatus:
    async def test_calls_execute_with_status_and_id(self):
        repo, pool = _make_repo()
        await repo.update_market_status("mkt-1", "settled")

        pool.execute.assert_called_once()
        args = pool.execute.call_args[0]
        assert args[1] == "settled"
        assert args[2] == "mkt-1"


# ---------------------------------------------------------------------------
# Price Snapshots
# ---------------------------------------------------------------------------

class TestInsertSnapshot:
    async def test_calls_execute_with_correct_params(self):
        repo, pool = _make_repo()
        ts = _utc()
        snap = {
            "ts": ts, "market_id": "m1",
            "best_bid": 0.47, "best_ask": 0.49, "mid_price": 0.48,
            "spread": 0.02, "bid_depth": 1500, "ask_depth": 1200,
            "volume_24h": 50000, "time_to_event_h": 10.5,
        }
        await repo.insert_snapshot(snap)

        args = pool.execute.call_args[0]
        assert args[1] == ts
        assert args[2] == "m1"
        assert args[3] == 0.47
        assert args[5] == 0.48
        assert args[10] == 10.5

    async def test_sql_has_on_conflict_do_nothing(self):
        repo, pool = _make_repo()
        await repo.insert_snapshot({
            "ts": _utc(), "market_id": "m1",
            "best_bid": None, "best_ask": None, "mid_price": None,
            "spread": None, "bid_depth": None, "ask_depth": None,
            "volume_24h": None, "time_to_event_h": None,
        })
        sql = pool.execute.call_args[0][0]
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

class TestInsertTrade:
    async def test_calls_execute_with_correct_params(self):
        repo, pool = _make_repo()
        ts = _utc()
        trade = {
            "ts": ts, "market_id": "m1",
            "trade_id": "t-999", "price": 0.52, "size": 100.0, "side": "buy",
        }
        await repo.insert_trade(trade)

        args = pool.execute.call_args[0]
        assert args[1] == ts
        assert args[2] == "m1"
        assert args[3] == "t-999"
        assert args[4] == 0.52
        assert args[6] == "buy"

    async def test_sql_has_on_conflict_do_nothing(self):
        repo, pool = _make_repo()
        await repo.insert_trade({
            "ts": _utc(), "market_id": "m1",
            "trade_id": "t-1", "price": 0.5, "size": 10, "side": "sell",
        })
        sql = pool.execute.call_args[0][0]
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql

    async def test_missing_trade_id_defaults_to_empty_string(self):
        repo, pool = _make_repo()
        await repo.insert_trade({"ts": _utc(), "market_id": "m1"})
        args = pool.execute.call_args[0]
        assert args[3] == ""  # trade_id default


# ---------------------------------------------------------------------------
# Spike Events
# ---------------------------------------------------------------------------

class TestInsertSpikeEvent:
    async def test_calls_execute_with_all_fields(self):
        repo, pool = _make_repo()
        start = _utc(minutes=-5)
        peak = _utc(minutes=-3)
        end = _utc(minutes=-1)
        event = {
            "market_id": "m1",
            "start_ts": start, "peak_ts": peak, "end_ts": end,
            "start_price": 0.50, "peak_price": 0.55, "end_price": 0.52,
            "magnitude": 0.05, "direction": "up", "n_steps": 5,
        }
        await repo.insert_spike_event(event)

        args = pool.execute.call_args[0]
        # $1=market_id $2=start_ts $3=peak_ts $4=end_ts $5=start_price
        # $6=peak_price $7=end_price $8=magnitude $9=direction $10=n_steps
        assert args[1] == "m1"
        assert args[2] == start
        assert args[8] == 0.05   # magnitude
        assert args[9] == "up"   # direction
        assert args[10] == 5     # n_steps


# ---------------------------------------------------------------------------
# Data Gaps
# ---------------------------------------------------------------------------

class TestInsertGap:
    async def test_calls_execute_with_correct_params(self):
        repo, pool = _make_repo()
        gap_start = _utc(minutes=-10)
        await repo.insert_gap("m1", gap_start, "ws_disconnect")

        args = pool.execute.call_args[0]
        assert args[1] == "m1"
        assert args[2] == gap_start
        assert args[3] == "ws_disconnect"


class TestCloseGap:
    async def test_calculates_minutes_correctly(self):
        repo, pool = _make_repo()
        gap_start = _utc(minutes=-30)
        gap_end = _utc(minutes=0)

        await repo.close_gap("m1", gap_start, gap_end)

        args = pool.execute.call_args[0]
        minutes_arg = args[2]
        assert args[3] == "m1"
        assert args[4] == gap_start
        # minutes should be ~30
        assert abs(minutes_arg - 30.0) < 1.0

    async def test_sql_updates_where_gap_end_is_null(self):
        repo, pool = _make_repo()
        start = _utc(minutes=-10)
        end = _utc()
        await repo.close_gap("m1", start, end)

        sql = pool.execute.call_args[0][0]
        assert "gap_end IS NULL" in sql
        assert "UPDATE data_gaps" in sql


# ---------------------------------------------------------------------------
# Pool close
# ---------------------------------------------------------------------------

class TestClose:
    async def test_calls_pool_close(self):
        repo, pool = _make_repo()
        await repo.close()
        pool.close.assert_called_once()
