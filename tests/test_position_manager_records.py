"""
Unit tests for trading/position_manager.py — T-37 asyncpg.Record handling.

These tests specifically model asyncpg.Record semantics (subscript-only, no
`.get()`). Earlier dict-based mocks missed bugs where callers wrote
`record.get("field")` which fails against a real Record. The RecordLike class
below reproduces that failure mode so regressions are caught at test time.

Run with:
    venv/bin/python -m pytest tests/test_position_manager_records.py -v
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from trading.position_manager import get_open_positions


class RecordLike:
    """Mimics asyncpg.Record: subscript-only, no `.get()`, iterable via keys/values.

    Calling `.get()` on an asyncpg.Record raises AttributeError in production.
    This fixture reproduces that so tests that accidentally treat a Record as
    a dict fail the same way they would in production.
    """

    def __init__(self, data: dict) -> None:
        self._data = dict(data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data.keys())

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __contains__(self, key):
        return key in self._data


def test_record_like_rejects_get() -> None:
    """Sanity-check the fixture — it must NOT expose `.get`. If this ever
    accidentally passes with hasattr(rec, 'get') = True, the rest of the
    test file is lying about catching `.get()` bugs."""
    rec = RecordLike({"id": 1, "fill_status": "filled"})
    assert not hasattr(rec, "get"), "RecordLike must not expose .get()"
    assert rec["fill_status"] == "filled"       # subscript works
    assert list(rec) == ["id", "fill_status"]   # iter works
    assert "fill_status" in rec                 # contains works


def test_record_like_convertible_to_dict() -> None:
    """dict(record) must produce a plain mapping — this is what the T-37 fix
    relies on. If asyncpg.Record ever removes this behavior, position_manager's
    `[dict(r) for r in rows]` breaks and we need to know immediately."""
    rec = RecordLike({"id": 42, "fill_status": "filled", "slug": "nba-gsw-lac"})
    d = dict(rec)
    assert d == {"id": 42, "fill_status": "filled", "slug": "nba-gsw-lac"}
    # The converted dict must support .get() — this is the contract callers rely on
    assert d.get("fill_status") == "filled"
    assert d.get("missing", "default") == "default"


def test_get_open_positions_returns_dicts_not_records() -> None:
    """Contract test: `get_open_positions` must return list[dict], not list[Record].

    Enforces the T-37 normalization so downstream callers (risk_guard,
    order_poller, telegram_commands) can safely call `.get(...)` on rows.
    """
    rows = [
        RecordLike({"id": 1, "fill_status": "filled", "token_id": "TOK_A"}),
        RecordLike({"id": 2, "fill_status": "pending", "token_id": "TOK_B"}),
    ]
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=rows)

    result = asyncio.run(get_open_positions(pool))

    assert len(result) == 2
    # Every returned row must be a dict (not RecordLike, not asyncpg.Record)
    for pos in result:
        assert isinstance(pos, dict), f"Expected dict, got {type(pos).__name__}"
        # And must support .get() — the API callers depend on
        assert pos.get("fill_status") in ("filled", "pending")
        assert pos.get("missing_field", "default") == "default"


def test_get_open_positions_preserves_all_columns() -> None:
    """Every column from the SQL result must survive the dict conversion."""
    row = RecordLike({
        "id": 123,
        "slug": "nba-bos-lal",
        "token_id": "0xTOK",
        "size_shares": 100.0,
        "entry_price": 0.42,
        "fill_status": "filled",
        "current_bid": 0.45,
        "exit_order_id": None,
        "notes": "tanking signal",
    })
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[row])

    result = asyncio.run(get_open_positions(pool))
    assert len(result) == 1
    pos = result[0]
    assert pos["id"] == 123
    assert pos["slug"] == "nba-bos-lal"
    assert pos.get("current_bid") == 0.45
    assert pos.get("exit_order_id") is None  # None stays None, not raised


def test_record_get_on_raw_record_would_fail() -> None:
    """Documents the bug we're preventing: calling `.get()` on a raw Record
    (not pre-converted to dict) raises AttributeError. This is the failure
    mode codex found in risk_guard, order_poller, and telegram_commands
    before T-37.
    """
    rec = RecordLike({"fill_status": "filled"})
    with pytest.raises(AttributeError):
        rec.get("fill_status")   # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────────────────
# T-38 — open_position() side parameter contract
# ─────────────────────────────────────────────────────────────────────────────


def _make_pool_with_fetchrow_capture(returning_id: int = 1) -> AsyncMock:
    """Pool mock that returns the given id and lets tests inspect SQL args."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value={"id": returning_id})
    return pool


def test_open_position_defaults_to_yes_side_for_backward_compat() -> None:
    """Callers that predate T-38 (tanking, prop) never pass `side`. The default
    must stay 'YES' so their behaviour does not regress."""
    from trading.position_manager import open_position

    pool = _make_pool_with_fetchrow_capture()
    asyncio.run(open_position(
        pool,
        market_id="mid-1", slug="nba-xyz", signal_type="tanking",
        token_id="TOK", size_usd=10.0, size_shares=25.0, entry_price=0.40,
        game_start=None, clob_order_id="ord1",
    ))
    # Pool was called — inspect the positional args passed to the INSERT
    args = pool.fetchrow.call_args.args
    # Args: (sql, market_id, slug, signal_type, side, token_id, ...)
    #       ( 0 ,     1    ,  2  ,      3     ,  4  ,    5    , ...)
    assert args[4] == "YES", f"Default side should be 'YES', got {args[4]!r}"


def test_open_position_persists_no_side_when_passed() -> None:
    """T-38 HIGH fix: when caller passes `side='NO'` (MLB NO-side path), the
    INSERT must carry 'NO' through to the DB, not silently substitute 'YES'."""
    from trading.position_manager import open_position

    pool = _make_pool_with_fetchrow_capture()
    asyncio.run(open_position(
        pool,
        market_id="mid-mlb", slug="mlb-lad-sf", signal_type="pitcher",
        token_id="NO_TOK", size_usd=10.0, size_shares=20.0, entry_price=0.53,
        game_start=None, clob_order_id="ord2",
        side="NO",
    ))
    args = pool.fetchrow.call_args.args
    assert args[4] == "NO", f"Caller-provided side 'NO' was dropped, got {args[4]!r}"


# ─────────────────────────────────────────────────────────────────────────────
# T-38 — calibration_signal build_edges `persist` flag contract
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Round-3 — DRY_ exit must close immediately (not leave position in exit_pending)
# ─────────────────────────────────────────────────────────────────────────────


def test_dry_run_exit_closes_position_immediately() -> None:
    """Round-3 HIGH fix: `_handle_exit_pending` must close positions whose
    exit_order_id starts with `DRY_`. Previously the code just returned,
    leaving open_positions stuck in fill_status='exit_pending' forever when
    running in dry-run mode. This test would have caught the bug.
    """
    from unittest.mock import patch
    from trading.order_poller import _handle_exit_pending

    # Simulate a position that had a dry-run SELL placed against it
    pos = {
        "id": 99,
        "slug": "nba-bos-lal",
        "market_id": "mid-lazy",
        "exit_order_id": "DRY_SELL_abc12345",
        "current_bid": 0.37,
        "entry_price": 0.42,
    }
    pool = AsyncMock()
    executor = AsyncMock()   # must not be called for DRY_ path

    with patch("trading.order_poller.close_position", new_callable=AsyncMock) as mock_close:
        mock_close.return_value = -2.5   # simulated P&L in USD
        asyncio.run(
            _handle_exit_pending(pos, pool, executor, "tg_tok", "chat_id")
        )

    # The DRY branch must call close_position(pool, position_id, exit_price)
    mock_close.assert_awaited_once()
    args = mock_close.await_args.args
    assert args[0] is pool
    assert args[1] == 99                              # position_id
    assert args[2] == 0.37                            # current_bid wins over entry_price

    # And it must NOT call executor.get_order — DRY_ never touched CLOB
    executor.get_order.assert_not_awaited()


def test_dry_run_exit_reverts_if_no_usable_price() -> None:
    """Defensive: if somehow both current_bid and entry_price are missing/zero,
    we must not close at $0 (that would book an outsized fake loss). Revert
    to filled instead so the next tick can retry."""
    from unittest.mock import patch
    from trading.order_poller import _handle_exit_pending

    pos = {
        "id": 100,
        "slug": "nba-bos-lal",
        "market_id": "mid-x",
        "exit_order_id": "DRY_SELL_zz",
        "current_bid": 0,
        "entry_price": 0,
    }
    pool = AsyncMock()
    executor = AsyncMock()

    with patch("trading.order_poller.close_position", new_callable=AsyncMock) as mock_close, \
         patch("trading.order_poller.mark_exit_failed", new_callable=AsyncMock) as mock_revert:
        asyncio.run(_handle_exit_pending(pos, pool, executor, "tok", "chat"))

    mock_close.assert_not_awaited()
    mock_revert.assert_awaited_once_with(pool, 100)


# ─────────────────────────────────────────────────────────────────────────────
# Round-5 — rejected-buy guard (_is_buy_rejected)
# ─────────────────────────────────────────────────────────────────────────────


def test_is_buy_rejected_empty_order_id() -> None:
    """Empty order_id means the CLOB rejected the buy — caller MUST NOT
    persist an open_position or the poller would retry forever."""
    from trading.bot_main import _is_buy_rejected

    rejected, reason = _is_buy_rejected({"order_id": "", "status": "rejected", "error": "insufficient balance"})
    assert rejected is True
    assert reason == "insufficient balance"


def test_is_buy_rejected_missing_order_id() -> None:
    """Some failure modes return a dict without order_id at all."""
    from trading.bot_main import _is_buy_rejected

    rejected, reason = _is_buy_rejected({"status": "rejected"})
    assert rejected is True
    assert reason == "rejected"


def test_is_buy_rejected_none_order_id() -> None:
    """py-clob-client occasionally returns None for order_id on failure."""
    from trading.bot_main import _is_buy_rejected

    rejected, reason = _is_buy_rejected({"order_id": None, "status": "failed", "error": "auth"})
    assert rejected is True
    assert reason == "auth"


def test_is_buy_rejected_accepted_order() -> None:
    """Non-empty order_id = success. Caller proceeds with open_position."""
    from trading.bot_main import _is_buy_rejected

    rejected, reason = _is_buy_rejected({"order_id": "0xABC123", "status": "matched", "price": 0.42})
    assert rejected is False
    assert reason == ""


def test_is_buy_rejected_dry_run_order_is_accepted() -> None:
    """DRY_BUY_xxx ids must count as accepted — dry-run mode simulates a fill
    and downstream persistence (with fake id) is intentional for paper trading."""
    from trading.bot_main import _is_buy_rejected

    rejected, reason = _is_buy_rejected({"order_id": "DRY_BUY_abcd1234", "status": "dry_run"})
    assert rejected is False
    assert reason == ""


def test_is_buy_rejected_reason_fallback_when_no_error_key() -> None:
    """If neither `error` nor `status` present, helper still returns a reason
    string (never None) so log lines and Telegram messages never get None."""
    from trading.bot_main import _is_buy_rejected

    rejected, reason = _is_buy_rejected({"order_id": ""})
    assert rejected is True
    assert reason == "unknown"


def test_build_edges_persist_false_skips_insert() -> None:
    """T-38 MED fix: `--dry-run` must not mutate calibration_edges.
    With persist=False, build_edges should still return computed edges but
    never call pool.execute() with an INSERT statement."""
    from analytics.calibration_signal import build_edges

    # Mock conn that captures every execute call
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=500)   # enough rows to proceed
    conn.fetch = AsyncMock(return_value=[
        {"sport": "basketball", "market_type": "moneyline", "price": 0.25, "outcome": 1},
        {"sport": "basketball", "market_type": "moneyline", "price": 0.25, "outcome": 0},
        {"sport": "basketball", "market_type": "moneyline", "price": 0.25, "outcome": 1},
    ])
    conn.execute = AsyncMock()

    # Pool that hands out the same mock conn from both `acquire()` calls.
    # `async with pool.acquire() as c` uses __aenter__/__aexit__.
    class _Ctx:
        async def __aenter__(self_inner): return conn
        async def __aexit__(self_inner, *a): return None

    pool = AsyncMock()
    pool.acquire = lambda: _Ctx()

    asyncio.run(build_edges(pool, persist=False))

    # The only pool.execute calls should be zero — DRY-RUN means no INSERT.
    for call in conn.execute.await_args_list:
        sql = call.args[0] if call.args else ""
        assert "INSERT INTO calibration_edges" not in sql, (
            f"persist=False must not execute INSERT; got: {sql[:60]!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Round-6 / T-42 — send_order_confirmation must render the real YES/NO side
# ─────────────────────────────────────────────────────────────────────────────


def _capture_post():
    """Helper: patch _post inside telegram_confirm and return the call args."""
    from unittest.mock import patch

    captured = {}

    async def fake_post(token, method, payload):
        captured["method"] = method
        captured["payload"] = payload

    return patch("trading.telegram_confirm._post", side_effect=fake_post), captured


def test_send_order_confirmation_renders_no_side_for_no_trade() -> None:
    """T-42 MED fix: NO-side fills were previously rendered as 'YES @ price'.
    After the fix, side='NO' must land verbatim in the message body."""
    from trading.telegram_confirm import send_order_confirmation

    patcher, captured = _capture_post()
    with patcher:
        asyncio.run(send_order_confirmation(
            token="tok", chat_id="chat",
            team_or_player="Dodgers",
            price=0.47, size_shares=20, size_usd=9.4,
            order_id="0xABC", status="matched",
            side="NO",
        ))

    assert captured["method"] == "sendMessage"
    text = captured["payload"]["text"]
    assert "Dodgers</b> NO @ 0.470" in text
    assert "Dodgers</b> YES @" not in text   # must not still claim YES


def test_send_order_confirmation_defaults_to_yes_when_side_omitted() -> None:
    """Back-compat: tanking and prop callers never pass `side`. The default
    stays 'YES' so their Telegram message body keeps reading 'YES @ price'."""
    from trading.telegram_confirm import send_order_confirmation

    patcher, captured = _capture_post()
    with patcher:
        asyncio.run(send_order_confirmation(
            token="tok", chat_id="chat",
            team_or_player="Lakers",
            price=0.38, size_shares=26, size_usd=9.9,
            order_id="0xDEF", status="matched",
        ))

    text = captured["payload"]["text"]
    assert "Lakers</b> YES @ 0.380" in text


def test_send_order_confirmation_rejects_garbage_side_value() -> None:
    """Defensive: if some future caller passes an invalid side (typo, None,
    empty string) we must not emit misleading garbage into Telegram. Fall
    back to 'YES' silently — the label is UX only; the true side is persisted
    in open_positions.side."""
    from trading.telegram_confirm import send_order_confirmation

    patcher, captured = _capture_post()
    with patcher:
        asyncio.run(send_order_confirmation(
            token="tok", chat_id="chat",
            team_or_player="Team", price=0.5, size_shares=10, size_usd=5,
            order_id="0xABC", status="matched",
            side="whatever",   # invalid
        ))

    text = captured["payload"]["text"]
    assert " YES @ 0.500" in text
    assert " whatever @" not in text


# ─────────────────────────────────────────────────────────────────────────────
# Round-6 / T-42 — _process_pitcher_signal must NOT double-flip NO-side price
# ─────────────────────────────────────────────────────────────────────────────


def _make_pitcher_signal(favored_side: str, current_price: float):
    """Build a minimal PitcherSignal. Scanner writes `current_price` already
    side-corrected (i.e. 1 - yes_mid for NO favorites) since T-41."""
    from datetime import datetime, timezone
    from analytics.mlb_pitcher_scanner import PitcherSignal

    return PitcherSignal(
        market_id="mlb-lad-sf",
        slug="mlb-lad-sf-2026-04-17",
        game_start=datetime(2026, 4, 17, 23, 0, tzinfo=timezone.utc),
        hours_to_game=10.0,
        favored_team="Dodgers",
        underdog_team="Giants",
        home_pitcher_name="A", home_pitcher_era=3.0, home_pitcher_whip=1.1,
        away_pitcher_name="B", away_pitcher_era=4.5, away_pitcher_whip=1.3,
        era_differential=1.5,
        quality_differential=2.0,
        current_price=current_price,
        price_24h_ago=None,
        actual_drift=None,
        signal_strength="HIGH",
        recommended_action="BUY",
        favored_side=favored_side,
    )


def test_process_pitcher_signal_uses_signal_price_for_no_side_directly() -> None:
    """T-42 HIGH fix: on a NO-side MLB signal, _process_pitcher_signal must
    feed `signal.current_price` (already side-correct per T-41 scanner change)
    straight into executor.buy / send_order_confirmation — NOT re-flip to
    1 - current_price, which would produce the underdog's YES price.

    The prior double-inversion bug would silently have sized + priced against
    the wrong series. We pin it by asserting the exact price handed to buy()
    and the `side` argument of the confirmation message.
    """
    from unittest.mock import patch, AsyncMock
    from trading import bot_main

    signal = _make_pitcher_signal(favored_side="NO", current_price=0.47)
    # trading.enabled=True so we hit the buy() path. min_ask_depth_usd is
    # tiny so check_entry passes.
    config = {
        "trading": {
            "enabled": True,
            "min_ask_depth_usd": 10,
            "max_total_exposure_usd": 100,
        },
    }

    executor = AsyncMock()
    executor.get_market_info = AsyncMock(return_value={
        "bid": 0.46, "ask": 0.48, "ask_depth_usd": 200.0,
    })
    # buy() returns a valid-looking order so _is_buy_rejected passes.
    executor.buy = AsyncMock(return_value={
        "order_id": "0xREAL", "status": "matched", "size_shares": 20.0,
    })

    pool = AsyncMock()

    with patch("trading.bot_main.resolve_team_token_side",
               new=AsyncMock(return_value=("NO_TOKEN_ID", "NO"))), \
         patch("trading.bot_main.correlation_check",
               new=AsyncMock(return_value=(False, ""))), \
         patch("trading.bot_main.can_open",
               return_value=(True, "")), \
         patch("trading.bot_main.check_entry",
               return_value=("enter", "ok", "✅")), \
         patch("trading.bot_main.position_size_by_ev",
               return_value=(10.0, 20.0)), \
         patch("trading.bot_main.send_signal_alert",
               new=AsyncMock()) as mock_signal_alert, \
         patch("trading.bot_main.open_position",
               new=AsyncMock(return_value=999)), \
         patch("trading.bot_main.log_order",
               new=AsyncMock()), \
         patch("trading.bot_main.send_order_confirmation",
               new=AsyncMock()) as mock_order_confirm:
        asyncio.run(bot_main._process_pitcher_signal(
            signal=signal,
            pool=pool,
            executor=executor,
            config=config,
            tg_token="tok",
            tg_chat_id="chat",
            total_exposure=0.0,
            mlb_aliases={},
        ))

    # ─── Critical assertion: executor.buy was called with the NO-side price ───
    buy_args = executor.buy.await_args
    token_arg = buy_args.args[0]
    price_arg = buy_args.args[1]
    assert token_arg == "NO_TOKEN_ID", f"wrong token_id: {token_arg!r}"
    # ask (0.48) beats signal price (0.47) in enter-mode. Accept either the
    # ask OR the signal price — the critical property is that we DID NOT flip
    # to 1 - 0.47 = 0.53 (the underdog's YES price).
    assert abs(price_arg - 0.48) < 1e-9 or abs(price_arg - 0.47) < 1e-9, (
        f"buy price {price_arg} looks double-inverted; expected 0.47/0.48, "
        f"not 0.53 (1 - signal.current_price)"
    )
    assert abs(price_arg - 0.53) > 1e-9, (
        f"REGRESSION: double-inversion re-introduced — price={price_arg} "
        f"equals 1 - signal.current_price for NO-side favorite"
    )

    # ─── Alert rendered with the correct price + side ─────────────────────────
    alert_kwargs = mock_signal_alert.await_args.kwargs
    assert abs(alert_kwargs["price"] - 0.47) < 1e-9, (
        f"signal alert showed {alert_kwargs['price']}, expected 0.47"
    )

    # ─── Telegram order confirmation receives side='NO' ───────────────────────
    confirm_kwargs = mock_order_confirm.await_args.kwargs
    assert confirm_kwargs["side"] == "NO", (
        f"order confirmation got side={confirm_kwargs['side']!r}, expected 'NO'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# T-36 — tanking scanner NO-side correctness
# ─────────────────────────────────────────────────────────────────────────────


def _make_tanking_signal(motivated_side: str, current_price: float):
    """Build a minimal TankingSignal. Scanner writes `current_price` already
    side-corrected (i.e. 1 - yes_mid for NO-side motivated teams) since T-36."""
    from datetime import datetime, timezone
    from analytics.tanking_scanner import TankingSignal

    return TankingSignal(
        market_id="nba-det-phi-2026-04-17",
        slug="nba-det-phi-2026-04-17",
        game_start=datetime(2026, 4, 17, 23, 0, tzinfo=timezone.utc),
        hours_to_game=10.0,
        motivated_team="Philadelphia 76ers",
        tanking_team="Detroit Pistons",
        motivation_differential=2.5,
        current_price=current_price,
        price_24h_ago=None,
        actual_drift=None,
        pattern_strength="HIGH",
        recommended_action="BUY",
        motivated_side=motivated_side,
    )


def test_process_tanking_signal_uses_signal_price_for_no_side_directly() -> None:
    """T-36 HIGH fix: on a NO-side tanking signal, _process_tanking_signal must
    feed `signal.current_price` (already side-correct per T-36 scanner change)
    straight into executor.buy / send_order_confirmation — NOT re-derive from
    a blind YES-token lookup, which would buy the tanking team's contract.

    Mirrors the T-42 test for MLB. Pin the exact price handed to buy() and
    the `side` argument of the confirmation message to catch any regression.
    """
    from unittest.mock import patch, AsyncMock
    from trading import bot_main

    signal = _make_tanking_signal(motivated_side="NO", current_price=0.72)
    config = {
        "trading": {
            "enabled": True,
            "min_ask_depth_usd": 10,
            "max_total_exposure_usd": 100,
        },
    }

    executor = AsyncMock()
    executor.get_market_info = AsyncMock(return_value={
        "bid": 0.71, "ask": 0.73, "ask_depth_usd": 200.0,
    })
    executor.buy = AsyncMock(return_value={
        "order_id": "0xTANK", "status": "matched", "size_shares": 13.0,
    })

    pool = AsyncMock()

    with patch("trading.bot_main.resolve_team_token_side",
               new=AsyncMock(return_value=("NO_TOKEN_ID", "NO"))), \
         patch("trading.bot_main.correlation_check",
               new=AsyncMock(return_value=(False, ""))), \
         patch("trading.bot_main.can_open",
               return_value=(True, "")), \
         patch("trading.bot_main.check_entry",
               return_value=("enter", "ok", "✅")), \
         patch("trading.bot_main.position_size_by_ev",
               return_value=(10.0, 13.0)), \
         patch("trading.bot_main.tanking_roi_estimate",
               return_value=5.0), \
         patch("trading.bot_main.send_signal_alert",
               new=AsyncMock()) as mock_signal_alert, \
         patch("trading.bot_main.open_position",
               new=AsyncMock(return_value=777)) as mock_open_position, \
         patch("trading.bot_main.log_order",
               new=AsyncMock()), \
         patch("trading.bot_main.send_order_confirmation",
               new=AsyncMock()) as mock_order_confirm:
        asyncio.run(bot_main._process_tanking_signal(
            signal=signal,
            pool=pool,
            executor=executor,
            config=config,
            tg_token="tok",
            tg_chat_id="chat",
            total_exposure=0.0,
            aliases={"76ers": "Philadelphia 76ers"},
        ))

    # Critical: executor.buy called with NO-side token + NO-side price (not flipped)
    buy_args = executor.buy.await_args
    token_arg = buy_args.args[0]
    price_arg = buy_args.args[1]
    assert token_arg == "NO_TOKEN_ID", f"wrong token_id: {token_arg!r}"
    # ask (0.73) beats signal price (0.72) in enter-mode; either is acceptable.
    # The guard: price must NOT be 1 - 0.72 = 0.28 (the tanking team's YES price).
    assert abs(price_arg - 0.73) < 1e-9 or abs(price_arg - 0.72) < 1e-9, (
        f"buy price {price_arg} looks double-inverted; expected 0.72/0.73"
    )
    assert abs(price_arg - 0.28) > 1e-9, (
        f"REGRESSION: tanking scanner's side-correct price was re-flipped — "
        f"price={price_arg} equals 1 - signal.current_price"
    )

    # Alert price is the signal's side-correct value (not re-flipped)
    alert_kwargs = mock_signal_alert.await_args.kwargs
    assert abs(alert_kwargs["price"] - 0.72) < 1e-9

    # open_position was called with side='NO' so DB audit matches execution
    open_kwargs = mock_open_position.await_args.kwargs
    assert open_kwargs["side"] == "NO", (
        f"open_position got side={open_kwargs['side']!r}, expected 'NO'"
    )
    assert open_kwargs["token_id"] == "NO_TOKEN_ID"

    # Telegram order confirmation receives side='NO'
    confirm_kwargs = mock_order_confirm.await_args.kwargs
    assert confirm_kwargs["side"] == "NO"


def test_process_tanking_signal_aborts_on_side_mismatch() -> None:
    """T-36 defensive: if scanner-time side disagrees with live-resolve side,
    refuse to trade rather than pick one. Mirror of the MLB mismatch test."""
    from unittest.mock import patch, AsyncMock
    from trading import bot_main

    signal = _make_tanking_signal(motivated_side="YES", current_price=0.60)
    config = {"trading": {"enabled": True, "min_ask_depth_usd": 10, "max_total_exposure_usd": 100}}
    executor = AsyncMock()
    executor.get_market_info = AsyncMock(return_value={"bid": 0.59, "ask": 0.61, "ask_depth_usd": 200.0})
    executor.buy = AsyncMock()

    with patch("trading.bot_main.resolve_team_token_side",
               new=AsyncMock(return_value=("SOME_TOKEN", "NO"))):
        added = asyncio.run(bot_main._process_tanking_signal(
            signal=signal, pool=AsyncMock(), executor=executor, config=config,
            tg_token="tok", tg_chat_id="chat", total_exposure=0.0,
            aliases={"76ers": "Philadelphia 76ers"},
        ))

    assert added == 0.0
    executor.buy.assert_not_awaited()


def test_process_pitcher_signal_aborts_on_side_mismatch() -> None:
    """T-42 defensive: if the scanner recorded favored_side='YES' but the live
    resolve returns 'NO' (token id remapped mid-flight, stale cache, etc.),
    we must refuse to trade rather than pick one and hope. The function
    returns 0.0 exposure and executor.buy is never called."""
    from unittest.mock import patch, AsyncMock
    from trading import bot_main

    signal = _make_pitcher_signal(favored_side="YES", current_price=0.40)
    config = {"trading": {"enabled": True, "min_ask_depth_usd": 10, "max_total_exposure_usd": 100}}
    executor = AsyncMock()
    executor.get_market_info = AsyncMock(return_value={"bid": 0.39, "ask": 0.41, "ask_depth_usd": 200.0})
    executor.buy = AsyncMock()   # must not be called

    with patch("trading.bot_main.resolve_team_token_side",
               new=AsyncMock(return_value=("SOME_TOKEN", "NO"))):
        added = asyncio.run(bot_main._process_pitcher_signal(
            signal=signal, pool=AsyncMock(), executor=executor, config=config,
            tg_token="tok", tg_chat_id="chat", total_exposure=0.0, mlb_aliases={},
        ))

    assert added == 0.0
    executor.buy.assert_not_awaited()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
