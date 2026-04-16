"""
Unit tests for trading/telegram_commands.py

Pure-logic tests only — no Telegram API calls, no DB, no CLOB.

Run with:
    pytest tests/test_telegram_commands.py -v
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Helpers imported directly (no I/O) ───────────────────────────────────────

from trading.telegram_commands import (
    _MAX_MSG,
    _cmd_pnl,
    _cmd_week,
    _send_long,
)


# ── _send_long: message splitting ────────────────────────────────────────────

class TestSendLong:
    """_send_long must never emit a chunk longer than _MAX_MSG chars."""

    @pytest.mark.asyncio
    async def test_short_message_sent_once(self):
        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, text: sent.append(text))):
            await _send_long("tok", "chat", "hello world")
        assert sent == ["hello world"]

    @pytest.mark.asyncio
    async def test_long_message_split_on_newline(self):
        # Build a message that exceeds _MAX_MSG but has clear newline boundaries
        line = "x" * 200 + "\n"
        text = line * ((_MAX_MSG // len(line)) + 5)   # definitely over limit
        assert len(text) > _MAX_MSG

        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, chunk: sent.append(chunk))):
            await _send_long("tok", "chat", text)

        assert len(sent) > 1, "should have been split into multiple messages"
        for chunk in sent:
            assert len(chunk) <= _MAX_MSG, f"chunk too long: {len(chunk)}"

    @pytest.mark.asyncio
    async def test_content_fully_preserved(self):
        line = "position-data-line-here\n"
        text = line * ((_MAX_MSG // len(line)) + 5)

        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, chunk: sent.append(chunk))):
            await _send_long("tok", "chat", text)

        reconstructed = "\n".join(sent)
        # Every original line must appear somewhere in the output
        for original_line in text.strip().splitlines():
            assert original_line in reconstructed

    @pytest.mark.asyncio
    async def test_exactly_at_limit_sent_once(self):
        text = "a" * _MAX_MSG
        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, chunk: sent.append(chunk))):
            await _send_long("tok", "chat", text)
        assert len(sent) == 1
        assert sent[0] == text

    @pytest.mark.asyncio
    async def test_no_chunk_exceeds_limit(self):
        """Stress test: 100 positions-worth of HTML-heavy content."""
        line = "<b>#42</b> some-market-slug-here\n  tanking | FILLED | $3.20 @ 0.085\n  Unrealized: <b>+$0.45</b>\n"
        text = line * 100

        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, chunk: sent.append(chunk))):
            await _send_long("tok", "chat", text)

        assert all(len(c) <= _MAX_MSG for c in sent)


# ── _cmd_pnl: by-strategy breakdown ──────────────────────────────────────────

class TestCmdPnl:
    def _make_pool(self, today_pnl=0.0, today_cnt=0,
                   all_pnl=0.0, all_closed=0, all_wins=0,
                   by_type=None):
        pool = AsyncMock()

        async def fetchrow(_sql, *args):
            row = MagicMock()
            if "CURRENT_DATE" in _sql:
                row.__getitem__ = lambda s, k: {"pnl": today_pnl, "cnt": today_cnt}.get(k)
            else:
                row.__getitem__ = lambda s, k: {"pnl": all_pnl, "closed": all_closed, "wins": all_wins}.get(k)
            return row

        async def fetch(_sql, *args):
            if by_type is None:
                return []
            rows = []
            for stype, pnl, cnt, wins in by_type:
                r = MagicMock()
                r.__getitem__ = lambda s, k, _t=stype, _p=pnl, _c=cnt, _w=wins: {
                    "signal_type": _t, "pnl": _p, "cnt": _c, "wins": _w,
                }.get(k)
                rows.append(r)
            return rows

        pool.fetchrow = fetchrow
        pool.fetch = fetch
        return pool

    @pytest.mark.asyncio
    async def test_no_trades_sends_message(self):
        pool = self._make_pool()
        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, text: sent.append(text))):
            await _cmd_pnl(pool, "tok", "chat")
        assert len(sent) == 1
        assert "P&L" in sent[0]

    @pytest.mark.asyncio
    async def test_by_strategy_section_present(self):
        pool = self._make_pool(
            all_pnl=10.0, all_closed=4, all_wins=3,
            by_type=[("tanking", 12.0, 3, 3), ("prop", -2.0, 1, 0)],
        )
        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, text: sent.append(text))):
            await _cmd_pnl(pool, "tok", "chat")
        assert len(sent) == 1
        msg = sent[0]
        assert "Tanking" in msg
        assert "Prop" in msg
        assert "$+12.00" in msg
        assert "$-2.00" in msg

    @pytest.mark.asyncio
    async def test_win_rate_shown(self):
        pool = self._make_pool(
            all_pnl=5.0, all_closed=2, all_wins=1,
            by_type=[("tanking", 5.0, 2, 1)],
        )
        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, text: sent.append(text))):
            await _cmd_pnl(pool, "tok", "chat")
        assert "50%" in sent[0]   # 1/2 win rate

    @pytest.mark.asyncio
    async def test_no_by_type_rows_hides_section(self):
        pool = self._make_pool(all_pnl=5.0, all_closed=1, all_wins=1, by_type=[])
        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, text: sent.append(text))):
            await _cmd_pnl(pool, "tok", "chat")
        assert "By strategy" not in sent[0]


# ── _cmd_week: day-by-day breakdown ──────────────────────────────────────────

class TestCmdWeek:
    def _make_pool(self, rows):
        """rows = list of (day, pnl, cnt, wins, types)"""
        pool = AsyncMock()

        async def fetch(sql, *args):
            result = []
            for day, pnl, cnt, wins, types in rows:
                r = MagicMock()
                r.__getitem__ = lambda s, k, _d=day, _p=pnl, _c=cnt, _w=wins, _t=types: {
                    "day": _d, "pnl": _p, "cnt": _c, "wins": _w, "types": _t,
                }.get(k)
                result.append(r)
            return result

        pool.fetch = fetch
        return pool

    @pytest.mark.asyncio
    async def test_no_trades_sends_empty_message(self):
        pool = self._make_pool([])
        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, text: sent.append(text))):
            await _cmd_week(pool, "tok", "chat")
        assert "last 7 days" in sent[0]

    @pytest.mark.asyncio
    async def test_days_appear_in_output(self):
        rows = [
            (date(2026, 4, 10), 3.50, 2, 2, "tanking"),
            (date(2026, 4, 11), -1.20, 1, 0, "prop"),
        ]
        pool = self._make_pool(rows)
        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, text: sent.append(text))):
            await _cmd_week(pool, "tok", "chat")
        msg = sent[0]
        assert "Apr 10" in msg
        assert "Apr 11" in msg
        assert "$+3.50" in msg
        assert "$-1.20" in msg

    @pytest.mark.asyncio
    async def test_totals_row_correct(self):
        rows = [
            (date(2026, 4, 10), 4.00, 2, 2, "tanking"),
            (date(2026, 4, 11), -1.00, 1, 0, "prop"),
        ]
        pool = self._make_pool(rows)
        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, text: sent.append(text))):
            await _cmd_week(pool, "tok", "chat")
        msg = sent[0]
        assert "$+3.00" in msg      # 4.00 - 1.00
        assert "67%" in msg         # 2/3 win rate

    @pytest.mark.asyncio
    async def test_green_emoji_for_positive_day(self):
        rows = [(date(2026, 4, 10), 5.00, 1, 1, "tanking")]
        pool = self._make_pool(rows)
        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, text: sent.append(text))):
            await _cmd_week(pool, "tok", "chat")
        assert "🟢" in sent[0]

    @pytest.mark.asyncio
    async def test_red_emoji_for_negative_day(self):
        rows = [(date(2026, 4, 10), -2.00, 1, 0, "prop")]
        pool = self._make_pool(rows)
        sent = []
        with patch("trading.telegram_commands._send", new=AsyncMock(side_effect=lambda t, c, text: sent.append(text))):
            await _cmd_week(pool, "tok", "chat")
        assert "🔴" in sent[0]
