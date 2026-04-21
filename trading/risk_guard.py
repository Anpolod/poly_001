"""
Risk Guard — runtime safety checks with DB + CLOB I/O.

Three independent guards:

  1. stop_loss_monitor(pool, executor, config, tg_token, tg_chat_id)
     Background asyncio task.
     Every stop_loss_check_interval_sec seconds fetches current bid for each
     confirmed open position. If bid dropped ≥ stop_loss_pct % below entry,
     places a market sell and closes the position.

  2. circuit_breaker_check(pool, config) → (blocked: bool, reason: str)
     Called before processing any signal.
     Blocks new entries if today's realised losses exceed daily_loss_limit_usd.
     Resets automatically at midnight UTC (no persistent state needed — always
     computed fresh from the DB).

  3. correlation_check(pool, config, game_start, sport) → (blocked: bool, reason: str)
     Called per signal before sending Telegram confirmation.
     Prevents opening too many positions on the same game (within 3-hour window)
     or in the same sport overall.

Config keys (settings.yaml → trading section):
    stop_loss_pct:            40.0   # exit if bid < entry × (1 - pct/100)
    stop_loss_check_sec:     300     # how often to poll (default 5 min)
    daily_loss_limit_usd:    20.0    # circuit breaker threshold
    max_positions_per_game:   2      # within a 3-hour game window
    max_positions_per_sport:  4      # across entire sport at once
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from trading.clob_executor import ClobExecutor
from trading.position_manager import (
    ensure_exit_order_id_column,
    get_open_positions,
    log_order,
    mark_exit_pending,
)

logger = logging.getLogger(__name__)

_GAME_WINDOW_HOURS = 3.0   # positions within this window count as "same game"

# ── T-48: bid sanity thresholds ──────────────────────────────────────────────
# Thin pre-game Polymarket books often show a lone "dust" bid at 0.01 (or
# other far-below-fair levels) while the ask side is still near fair value.
# Treating that bid as a real price triggers a false stop-loss, cascades into
# DRY_SELL + close_position at $0.01, and records a ~95% loss that never
# actually happened.
#
# Sanity rule: if bid is far below entry AND the spread is "implausibly wide"
# (ask >> bid by more than _MAX_SPREAD_RATIO), we treat the quote as an
# orphan dust bid and skip this tick. A legitimate real-market collapse
# would pull BOTH sides of the book down — bid and ask would stay close
# together. A wide spread means only ONE side moved, which is the signature
# of a stale / thin / dust quote.
#
# Longshot positions (entry < _BID_FLOOR_MIN_ENTRY) genuinely CAN collapse
# to $0.01 — we don't apply the guard there.
_BID_FLOOR_RATIO = 0.3          # bid < entry*this ⇒ candidate "too low"
_BID_FLOOR_MIN_ENTRY = 0.10     # only guard middle-of-market positions
_MAX_SPREAD_RATIO = 3.0         # ask/bid > this ⇒ quote untrusted


# T-49: renamed from `_bid_looks_orphan` to `bid_looks_orphan` (public) so
# `trading.exit_monitor` and any future exit path can import the same guard.
# Single source of truth: "is this CLOB quote trustworthy enough to trade on?"
def bid_looks_orphan(bid: float, ask: float, entry_price: float) -> bool:
    """Return True if the CLOB quote looks like a dust bid on a thin book
    rather than a real price collapse. Used to suppress false stop-losses
    (risk_guard) AND false stagnation exits (exit_monitor).
    """
    if entry_price < _BID_FLOOR_MIN_ENTRY:
        return False   # longshot: 0.01 might be the real price
    if bid <= 0:
        return False   # handled by earlier `bid <= 0` guard; not our concern
    if bid >= entry_price * _BID_FLOOR_RATIO:
        return False   # bid still within a reasonable range of entry
    if ask <= 0:
        return True    # bid far below entry AND no ask — no real market
    # Bid < 30% of entry. Is ask consistent (real crash) or not (dust)?
    if ask > bid * _MAX_SPREAD_RATIO:
        return True    # wide spread → dust bid, not a real collapse
    return False


# ── T-52: ask sanity — symmetric entry-side guard ────────────────────────────
# Post-mortem of pitcher-signal losses showed a second failure mode: we were
# entering positions 12-17h before game_start, when the book is still
# bid=0.01 ask=0.99 (dead). `entry_filter.check_entry` routed these to
# decision="limit" — in live trading that means a GTC at signal_price which
# never fills, but in dry_run the fake fill at signal_price lied to the
# accounting. Had it been live, we would have filled at ask=0.99 for
# something worth ~0.50 — an instant 98% paper loss.
#
# T-56: implemented as a strict complement of bid_looks_orphan so the
# mirror claim in the comment is mechanical rather than hand-tuned. For a
# market where YES trades around signal_price, the NO-side bid is (1-ask)
# and the NO-side "entry" is (1-signal_price); calling bid_looks_orphan on
# those complement values answers the same "is the contract we want to buy
# trading in a dead book?" question symmetrically across favorites,
# underdogs, and longshots.


def ask_looks_orphan(bid: float, ask: float, signal_price: float) -> bool:
    """Return True if the ask looks like a dust placeholder at the top of a
    thin pre-game book rather than a real seller. Used at ENTRY to refuse
    trading into books that have no real counterparty.
    """
    if ask <= 0 or ask >= 1.0:
        return False   # degenerate quote — not our concern here
    # Strict mirror: run bid_looks_orphan on the (1-price) complement side.
    # When bid<=0 we also want to delegate — let bid_looks_orphan decide
    # based on its own zero-bid rule applied to the complement.
    comp_bid = 1.0 - ask
    comp_ask = (1.0 - bid) if bid > 0 else 0.0
    comp_entry = 1.0 - signal_price
    return bid_looks_orphan(comp_bid, comp_ask, comp_entry)


# ── 1. Stop-loss monitor ──────────────────────────────────────────────────────

async def stop_loss_monitor(
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
) -> None:
    """Background task: sell positions that hit stop-loss or take-profit thresholds."""
    t = config["trading"]
    stop_loss_pct = float(t.get("stop_loss_pct", 40.0))
    take_profit_pct = float(t.get("take_profit_pct", 0.0))   # 0 = disabled
    interval = int(t.get("stop_loss_check_sec", 300))
    sl_threshold = 1.0 - stop_loss_pct / 100.0   # e.g. 40% loss → 0.60
    tp_threshold = 1.0 + take_profit_pct / 100.0 if take_profit_pct > 0 else None

    # One-time schema migrations: dashboard fields + T-35 exit_order_id
    try:
        await pool.execute(
            "ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS current_bid FLOAT DEFAULT NULL"
        )
    except Exception as exc:
        logger.warning("Could not add current_bid column: %s", exc)
    try:
        await ensure_exit_order_id_column(pool)
    except Exception as exc:
        logger.warning("Could not add exit_order_id column: %s", exc)

    logger.info(
        "Stop-loss/take-profit monitor started (SL=%.0f%%, TP=%s, interval=%ds)",
        stop_loss_pct,
        f"{take_profit_pct:.0f}%" if tp_threshold else "disabled",
        interval,
    )

    while True:
        try:
            positions = await get_open_positions(pool)
            # Only check confirmed fills — pending/ghost orders have no shares to sell
            filled = [
                p for p in positions
                if p["token_id"]
                and p["clob_order_id"]
                and (p.get("fill_status") or "pending") == "filled"
            ]

            for pos in filled:
                position_id = pos["id"]
                token_id = pos["token_id"]
                entry_price = float(pos["entry_price"] or 0)
                size_shares = float(pos["size_shares"] or 0)
                slug = pos["slug"] or pos["market_id"]

                if entry_price <= 0 or size_shares <= 0:
                    continue

                # T-48: fetch both bid+ask so we can sanity-check the quote.
                # Previous code used get_best_bid which returned the raw top-of-book,
                # even when that was an orphan dust bid on an otherwise thin book.
                try:
                    info = await executor.get_market_info(token_id)
                    bid = float(info.get("bid") or 0.0)
                    ask = float(info.get("ask") or 0.0)
                except Exception:
                    continue

                if bid <= 0:
                    continue

                if bid_looks_orphan(bid, ask, entry_price):
                    # Dust bid on thin book — neither update current_bid nor fire
                    # stop-loss. Dashboard would otherwise show a phantom -90%
                    # unrealized loss; risk_guard would have placed a DRY_SELL
                    # (or real SELL) at 0.01 and locked in the fake loss. Log
                    # so operator can see the suspicious quote without action.
                    logger.warning(
                        "%s: orphan dust quote skipped (bid=%.3f ask=%.3f entry=%.3f) — "
                        "thin book, not a real collapse",
                        slug, bid, ask, entry_price,
                    )
                    continue

                # Cache current_bid so dashboard can compute unrealized P&L
                try:
                    await pool.execute(
                        "UPDATE open_positions SET current_bid=$1 WHERE id=$2",
                        bid, position_id,
                    )
                except Exception:
                    pass

                # ── Take-profit ───────────────────────────────────────────────
                # T-35 P1.1: place SELL, mark exit_pending, let order_poller
                # finalise close_position once the SELL is MATCHED on CLOB.
                if tp_threshold is not None and bid >= entry_price * tp_threshold:
                    gain_pct = (bid - entry_price) / entry_price * 100
                    logger.info(
                        "TAKE-PROFIT triggered: %s  entry=%.3f  bid=%.3f  +%.1f%% (placing SELL)",
                        slug, entry_price, bid, gain_pct,
                    )
                    sell = await executor.sell(token_id, bid, size_shares)
                    sell_order_id = sell.get("order_id") or ""
                    await log_order(pool, pos["market_id"], position_id, "take_profit", sell)

                    if not sell_order_id:
                        # SELL was rejected outright — keep the position filled
                        # so the next tick can retry. Telegram alert with error.
                        logger.error(
                            "Take-profit SELL rejected for %s: %s",
                            slug, sell.get("error") or sell.get("status"),
                        )
                        try:
                            from trading.telegram_confirm import _post  # noqa: PLC0415
                            msg = (
                                f"⚠️ <b>Take-Profit SELL Rejected</b>\n"
                                f"Market: {slug}\n"
                                f"Will retry next stop-loss tick. "
                                f"Error: {sell.get('error') or sell.get('status') or 'unknown'}"
                            )
                            await _post(tg_token, "sendMessage", {
                                "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML",
                            })
                        except Exception:
                            pass
                        continue

                    await mark_exit_pending(pool, position_id, sell_order_id, reason=f"TP +{gain_pct:.1f}%")

                    try:
                        from trading.telegram_confirm import _post  # noqa: PLC0415
                        msg = (
                            f"💰 <b>Take-Profit Triggered (pending fill)</b>\n"
                            f"Market: {slug}\n"
                            f"Entry: {entry_price:.3f}  →  Bid: {bid:.3f}  (+{gain_pct:.1f}%)\n"
                            f"SELL order: <code>{sell_order_id[:20]}</code>\n"
                            f"Will close position once CLOB confirms fill."
                        )
                        await _post(tg_token, "sendMessage", {
                            "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML",
                        })
                    except Exception:
                        pass
                    continue   # exit pending — skip stop-loss check

                # ── Stop-loss ────────────────────────────────────────────────
                if bid >= entry_price * sl_threshold:
                    continue

                drop_pct = (entry_price - bid) / entry_price * 100
                logger.warning(
                    "STOP-LOSS triggered: %s  entry=%.3f  bid=%.3f  drop=%.1f%% (placing SELL)",
                    slug, entry_price, bid, drop_pct,
                )

                sell = await executor.sell(token_id, bid, size_shares)
                sell_order_id = sell.get("order_id") or ""
                await log_order(pool, pos["market_id"], position_id, "stop_loss", sell)

                if not sell_order_id:
                    logger.error(
                        "Stop-loss SELL rejected for %s: %s",
                        slug, sell.get("error") or sell.get("status"),
                    )
                    try:
                        from trading.telegram_confirm import _post  # noqa: PLC0415
                        msg = (
                            f"⚠️ <b>Stop-Loss SELL Rejected</b>\n"
                            f"Market: {slug}\n"
                            f"Position still held — will retry next tick. "
                            f"Error: {sell.get('error') or sell.get('status') or 'unknown'}"
                        )
                        await _post(tg_token, "sendMessage", {
                            "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML",
                        })
                    except Exception:
                        pass
                    continue

                await mark_exit_pending(pool, position_id, sell_order_id, reason=f"SL -{drop_pct:.1f}%")

                try:
                    from trading.telegram_confirm import _post  # noqa: PLC0415
                    msg = (
                        f"🛑 <b>Stop-Loss Triggered (pending fill)</b>\n"
                        f"Market: {slug}\n"
                        f"Entry: {entry_price:.3f}  →  Bid: {bid:.3f}  (−{drop_pct:.1f}%)\n"
                        f"SELL order: <code>{sell_order_id[:20]}</code>\n"
                        f"Will close position once CLOB confirms fill."
                    )
                    await _post(tg_token, "sendMessage", {
                        "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML",
                    })
                except Exception:
                    pass

        except asyncio.CancelledError:
            logger.info("Stop-loss/take-profit monitor cancelled.")
            return
        except Exception as exc:
            logger.error("Stop-loss monitor error: %s", exc)

        await asyncio.sleep(interval)


# ── 2. Circuit breaker ────────────────────────────────────────────────────────

async def circuit_breaker_check(
    pool: asyncpg.Pool,
    config: dict,
) -> tuple[bool, str]:
    """Return (blocked, reason) if today's realised losses exceed the daily limit.

    Always computed from DB — no in-memory state, so restart-safe.
    """
    limit = float(config["trading"].get("daily_loss_limit_usd", 20.0))

    row = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(pnl_usd), 0) AS today_pnl
        FROM open_positions
        WHERE status = 'closed'
          AND exit_ts >= CURRENT_DATE
        """
    )
    today_pnl = float(row["today_pnl"] or 0)

    if today_pnl <= -limit:
        return True, (
            f"circuit breaker: daily loss ${today_pnl:.2f} "
            f"exceeds limit −${limit:.2f}"
        )
    return False, ""


# ── 3. Correlation guard ──────────────────────────────────────────────────────

async def correlation_check(
    pool: asyncpg.Pool,
    config: dict,
    game_start: Optional[datetime],
    market_id: str,
) -> tuple[bool, str]:
    """Return (blocked, reason) if position limits by game or sport are exceeded.

    Joins open_positions → markets to get sport for the current signal.
    """
    t = config["trading"]
    max_per_game = int(t.get("max_positions_per_game", 2))
    max_per_sport = int(t.get("max_positions_per_sport", 4))

    # Get sport for this market
    mkt = await pool.fetchrow(
        "SELECT sport FROM markets WHERE id=$1", market_id
    )
    sport = mkt["sport"] if mkt else None

    # Check same-game concentration (positions within _GAME_WINDOW_HOURS)
    if game_start is not None:
        gs = game_start.replace(tzinfo=timezone.utc) if game_start.tzinfo is None else game_start
        game_count = await pool.fetchval(
            """
            SELECT COUNT(*)
            FROM open_positions
            WHERE status = 'open'
              AND game_start IS NOT NULL
              AND ABS(EXTRACT(EPOCH FROM (game_start - $1)) / 3600) < $2
            """,
            gs,
            _GAME_WINDOW_HOURS,
        )
        if (game_count or 0) >= max_per_game:
            return True, (
                f"correlation: already {game_count} position(s) on this game "
                f"(max {max_per_game})"
            )

    # Check per-sport concentration
    if sport:
        sport_count = await pool.fetchval(
            """
            SELECT COUNT(*)
            FROM open_positions op
            JOIN markets m ON op.market_id = m.id
            WHERE op.status = 'open'
              AND m.sport = $1
            """,
            sport,
        )
        if (sport_count or 0) >= max_per_sport:
            return True, (
                f"correlation: already {sport_count} {sport} position(s) "
                f"(max {max_per_sport})"
            )

    return False, ""
