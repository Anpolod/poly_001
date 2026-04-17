"""
Order Poller — monitors CLOB order fill status every 60 seconds.

Runs as a background asyncio.Task inside bot_main.run_loop().

For each open position:
  - clob_order_id = '': retry placing the buy order (if game hasn't started)
  - clob_order_id set: query CLOB for fill status
    - MATCHED  → update notes, send Telegram "✅ Filled"
    - CANCELLED/UNMATCHED → mark position cancelled, Telegram "❌ Order cancelled"
    - LIVE (still open) → do nothing this cycle

Auto-cancel: if a GTC order has been sitting unfilled for > max_pending_hours
and game starts in < 1 hour, cancel it and mark position cancelled.

Usage (from bot_main.py):
    asyncio.create_task(poll_order_fills(pool, executor, tg_token, tg_chat_id, config))
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import asyncpg

from trading.clob_executor import ClobExecutor
from trading.position_manager import (
    close_position,
    get_open_positions,
    log_order,
    mark_exit_failed,
    mark_position_cancelled,
    update_clob_order_id,
)

logger = logging.getLogger(__name__)

# How long a GTC order may sit open before we auto-cancel it when the game nears
_DEFAULT_MAX_PENDING_HOURS = 6
# Hours before game_start at which we force-cancel unfilled orders
_CANCEL_HOURS_BEFORE_GAME = 1.0
# Re-pricing defaults
_REPRICE_AFTER_HOURS = 2.0    # reprice if LIVE order older than this
_REPRICE_THRESHOLD_PCT = 5.0  # reprice if ask moved >5% from entry

_POLL_INTERVAL_SEC = 60


async def _ensure_fill_status_column(pool: asyncpg.Pool) -> None:
    """Add fill_status column to open_positions if it doesn't exist yet."""
    await pool.execute(
        """
        ALTER TABLE open_positions
        ADD COLUMN IF NOT EXISTS fill_status TEXT DEFAULT 'pending'
        """
    )


async def _set_fill_status(pool: asyncpg.Pool, position_id: int, fill_status: str) -> None:
    await pool.execute(
        "UPDATE open_positions SET fill_status=$1 WHERE id=$2",
        fill_status, position_id,
    )


async def _handle_no_order_id(
    pos: asyncpg.Record,
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    tg_token: str,
    tg_chat_id: str,
) -> None:
    """Retry placing a buy order for a position that has no clob_order_id."""
    position_id = pos["id"]
    slug = pos["slug"] or pos["market_id"]
    token_id = pos["token_id"] or ""
    entry_price = float(pos["entry_price"] or 0)
    size_usd = float(pos["size_usd"] or 0)
    game_start = pos["game_start"]

    if not token_id:
        logger.warning("Position %d (%s): no token_id, cannot retry order", position_id, slug)
        return

    if entry_price <= 0 or size_usd <= 0:
        logger.warning("Position %d (%s): invalid price/size, skipping retry", position_id, slug)
        return

    # Don't retry if game already started
    now = datetime.now(timezone.utc)
    if game_start and game_start.replace(tzinfo=timezone.utc) <= now:
        logger.info("Position %d (%s): game started, cancelling ghost position", position_id, slug)
        await mark_position_cancelled(pool, position_id)
        await _set_fill_status(pool, position_id, "stale")
        return

    logger.info("Position %d (%s): retrying BUY order @ %.4f  $%.2f",
                position_id, slug, entry_price, size_usd)

    order = await executor.buy(token_id, entry_price, size_usd)
    order_id = order.get("order_id", "")
    status = order.get("status", "")

    if order_id:
        await update_clob_order_id(pool, position_id, order_id)
        await _set_fill_status(pool, position_id, "pending")
        await log_order(pool, pos["market_id"], position_id, "buy_retry", order)
        logger.info("Position %d (%s): retry order placed — id=%s", position_id, slug, order_id[:16])
    else:
        error = order.get("error", "unknown")
        logger.warning("Position %d (%s): retry rejected (status=%s) — %s", position_id, slug, status, error)
        await log_order(pool, pos["market_id"], position_id, "buy_retry", order)
        try:
            from trading.telegram_confirm import _post  # noqa: PLC0415
            msg = (
                f"⚠️ <b>Order Retry Failed</b>\n"
                f"Market: {slug}\n"
                f"CLOB rejected retry: {error or status}"
            )
            await _post(tg_token, "sendMessage", {
                "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML"
            })
        except Exception:
            pass


async def _handle_existing_order(
    pos: asyncpg.Record,
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    tg_token: str,
    tg_chat_id: str,
    config: dict,
) -> None:
    """Check fill status of an order that has a clob_order_id."""
    position_id = pos["id"]
    order_id = pos["clob_order_id"]
    slug = pos["slug"] or pos["market_id"]
    game_start = pos["game_start"]

    order_data = await executor.get_order(order_id)
    if not order_data:
        logger.debug("Position %d: get_order returned empty for %s", position_id, order_id[:16])
        return

    clob_status = (order_data.get("status") or "").upper()
    logger.debug("Position %d (%s): CLOB status=%s", position_id, slug, clob_status)

    if clob_status == "MATCHED":
        # Order fully filled
        size_matched = float(order_data.get("sizeMatched") or order_data.get("size_matched") or 0)
        logger.info("Position %d (%s): FILLED  %.4f shares", position_id, slug, size_matched)

        await _set_fill_status(pool, position_id, "filled")
        if size_matched > 0:
            await pool.execute(
                "UPDATE open_positions SET size_shares=$1, notes=COALESCE(notes,'')||' [FILLED]' WHERE id=$2",
                size_matched, position_id,
            )

        try:
            from trading.telegram_confirm import _post  # noqa: PLC0415
            msg = (
                f"✅ <b>Order Filled</b>\n"
                f"Market: {slug}\n"
                f"Filled: {size_matched:.2f} shares\n"
                f"Order: <code>{order_id[:20]}</code>"
            )
            await _post(tg_token, "sendMessage", {
                "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML"
            })
        except Exception:
            logger.info("Telegram fill notification failed (non-critical)")

    elif clob_status in ("CANCELLED", "UNMATCHED"):
        logger.info("Position %d (%s): order CANCELLED/UNMATCHED on CLOB", position_id, slug)
        await mark_position_cancelled(pool, position_id)
        await _set_fill_status(pool, position_id, "cancelled")

        try:
            from trading.telegram_confirm import _post  # noqa: PLC0415
            msg = (
                f"❌ <b>Order Cancelled</b>\n"
                f"Market: {slug}\n"
                f"Order <code>{order_id[:20]}</code> was cancelled by CLOB."
            )
            await _post(tg_token, "sendMessage", {
                "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML"
            })
        except Exception:
            logger.info("Telegram cancel notification failed (non-critical)")

    elif clob_status == "LIVE":
        now = datetime.now(timezone.utc)
        t = config.get("trading", {})
        reprice_after_h = float(t.get("reprice_after_hours", _REPRICE_AFTER_HOURS))
        reprice_threshold = float(t.get("reprice_threshold_pct", _REPRICE_THRESHOLD_PCT)) / 100.0

        # ── Force-cancel if game starts too soon ─────────────────────────────
        if game_start:
            gs = game_start.replace(tzinfo=timezone.utc) if game_start.tzinfo is None else game_start
            hours_to_game = (gs - now).total_seconds() / 3600
            if hours_to_game < _CANCEL_HOURS_BEFORE_GAME:
                logger.info(
                    "Position %d (%s): %.1fh to game, force-cancelling unfilled order",
                    position_id, slug, hours_to_game,
                )
                cancelled = await executor.cancel(order_id)
                if cancelled:
                    await mark_position_cancelled(pool, position_id)
                    await _set_fill_status(pool, position_id, "force_cancelled")
                    try:
                        from trading.telegram_confirm import _post  # noqa: PLC0415
                        msg = (
                            f"⏰ <b>Order Force-Cancelled</b>\n"
                            f"Market: {slug}\n"
                            f"Game starts in {hours_to_game:.1f}h — unfilled order cancelled."
                        )
                        await _post(tg_token, "sendMessage", {
                            "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML"
                        })
                    except Exception:
                        pass
                return

        # ── Common fields needed for timeout + reprice ───────────────────────
        entry_ts = pos["entry_ts"]
        entry_price = float(pos["entry_price"] or 0)
        token_id = pos["token_id"] or ""
        size_usd = float(pos["size_usd"] or 0)

        if not token_id or entry_price <= 0:
            return

        if entry_ts:
            ets = entry_ts.replace(tzinfo=timezone.utc) if entry_ts.tzinfo is None else entry_ts
            order_age_h = (now - ets).total_seconds() / 3600
        else:
            order_age_h = 0.0

        # ── Timeout: cancel orders that have been sitting too long ────────────
        max_pending_h = float(t.get("max_pending_hours", _DEFAULT_MAX_PENDING_HOURS))
        if order_age_h >= max_pending_h:
            logger.info(
                "Position %d (%s): order timed out (%.1fh >= %.1fh max), cancelling",
                position_id, slug, order_age_h, max_pending_h,
            )
            cancelled = await executor.cancel(order_id)
            if cancelled:
                await mark_position_cancelled(pool, position_id)
                await _set_fill_status(pool, position_id, "timed_out")
                try:
                    from trading.telegram_confirm import _post  # noqa: PLC0415
                    msg = (
                        f"⌛ <b>Order Timed Out</b>\n"
                        f"Market: {slug}\n"
                        f"Unfilled after {order_age_h:.1f}h — cancelled."
                    )
                    await _post(tg_token, "sendMessage", {
                        "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML"
                    })
                except Exception:
                    pass
            return

        # ── Re-pricing: cancel + re-place if order is stale and market moved ─
        if order_age_h < reprice_after_h:
            return  # too young — leave it alone

        # Check how far the market has moved from our limit price
        try:
            info = await executor.get_market_info(token_id)
            current_ask = info["ask"]
        except Exception:
            return

        if current_ask <= 0:
            return

        move = abs(current_ask - entry_price) / entry_price
        if move < reprice_threshold:
            logger.debug(
                "Position %d (%s): LIVE %.1fh, move=%.1f%% < threshold — no reprice",
                position_id, slug, order_age_h, move * 100,
            )
            return

        # Market moved enough — cancel old order and re-place at current ask
        logger.info(
            "Position %d (%s): re-pricing — %.3f→%.3f (%.1f%% move, %.1fh old)",
            position_id, slug, entry_price, current_ask, move * 100, order_age_h,
        )
        cancelled = await executor.cancel(order_id)
        if not cancelled:
            logger.warning("Position %d: cancel failed before reprice", position_id)
            return

        # Mark old order gone immediately so next poll cycle doesn't double-cancel
        await update_clob_order_id(pool, position_id, "")
        await _set_fill_status(pool, position_id, "repricing")

        new_order = await executor.buy(token_id, current_ask, size_usd)
        new_order_id = new_order.get("order_id", "")

        if new_order_id:
            await update_clob_order_id(pool, position_id, new_order_id)
            # Update entry_price in DB to reflect the new limit
            await pool.execute(
                "UPDATE open_positions SET entry_price=$1 WHERE id=$2",
                current_ask, position_id,
            )
            await _set_fill_status(pool, position_id, "pending")
            await log_order(pool, pos["market_id"], position_id, "reprice", new_order)
            logger.info("Position %d (%s): re-priced → new order %s", position_id, slug, new_order_id[:16])
            try:
                from trading.telegram_confirm import _post  # noqa: PLC0415
                msg = (
                    f"🔄 <b>Order Re-Priced</b>\n"
                    f"Market: {slug}\n"
                    f"Old: {entry_price:.3f} → New: {current_ask:.3f} "
                    f"({move*100:+.1f}%, {order_age_h:.1f}h old)\n"
                    f"New order: <code>{new_order_id[:20]}</code>"
                )
                await _post(tg_token, "sendMessage", {
                    "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML"
                })
            except Exception:
                pass
        else:
            logger.warning("Position %d (%s): reprice order rejected", position_id, slug)
            await log_order(pool, pos["market_id"], position_id, "reprice", new_order)


async def _handle_exit_pending(
    pos: asyncpg.Record,
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    tg_token: str,
    tg_chat_id: str,
) -> None:
    """T-35 — finalise positions whose exit SELL is pending CLOB fill.

    Lifecycle:
      filled → exit_pending → closed   (happy path: SELL MATCHED)
      filled → exit_pending → filled    (revert: SELL CANCELLED/UNMATCHED — retry next tick)
    """
    position_id = pos["id"]
    slug = pos["slug"] or pos["market_id"]
    sell_order_id = pos["exit_order_id"] or ""
    if not sell_order_id:
        # No exit_order_id stored — defensive revert so the position doesn't
        # stay stuck in exit_pending forever.
        logger.warning("Position %d (%s): exit_pending with no exit_order_id, reverting", position_id, slug)
        await mark_exit_failed(pool, position_id)
        return

    if sell_order_id.startswith("DRY_"):
        # Round-3 fix: dry-run sells never hit CLOB, so there's nothing to poll.
        # Close the position immediately at the simulated exit price
        # (current_bid if we have it, entry_price as safe fallback) so the
        # ledger doesn't stay stuck in exit_pending forever. Without this,
        # every dry-run stop-loss/take-profit would pin open_positions open
        # indefinitely, inflating exposure and blocking new entries.
        exit_price = float(pos.get("current_bid") or pos.get("entry_price") or 0)
        if exit_price <= 0:
            logger.warning(
                "Position %d (%s): DRY exit with no usable price — reverting to filled",
                position_id, slug,
            )
            await mark_exit_failed(pool, position_id)
            return
        pnl = await close_position(pool, position_id, exit_price)
        logger.info(
            "Position %d (%s): DRY-RUN exit closed @ %.4f  P&L (simulated): %+.2f USD",
            position_id, slug, exit_price, pnl,
        )
        return

    order_data = await executor.get_order(sell_order_id)
    if not order_data:
        logger.debug("Position %d: get_order empty for exit %s", position_id, sell_order_id[:16])
        return

    clob_status = (order_data.get("status") or "").upper()
    logger.debug("Position %d (%s): exit SELL CLOB status=%s", position_id, slug, clob_status)

    if clob_status == "MATCHED":
        # SELL fully filled — finalise close_position with the actual matched price
        size_matched = float(order_data.get("sizeMatched") or order_data.get("size_matched") or 0)
        # py-clob-client doesn't always echo the fill price — fall back to the
        # bid we placed at, which is stored in entry_price of the sell order log.
        # For now, use the cached current_bid as a best-effort exit price.
        exit_price = float(pos.get("current_bid") or pos.get("entry_price") or 0)
        if exit_price <= 0:
            logger.warning("Position %d (%s): MATCHED exit but no fill price available, using entry as fallback", position_id, slug)
            exit_price = float(pos["entry_price"] or 0)

        pnl = await close_position(pool, position_id, exit_price)
        logger.info("Position %d (%s): exit MATCHED  shares=%.4f  pnl=$%+.2f", position_id, slug, size_matched, pnl)

        try:
            from trading.telegram_confirm import _post  # noqa: PLC0415
            msg = (
                f"✅ <b>Exit Filled</b>\n"
                f"Market: {slug}\n"
                f"Sold {size_matched:.2f} shares @ {exit_price:.3f}\n"
                f"P&L: <b>${pnl:+.2f}</b>"
            )
            await _post(tg_token, "sendMessage", {
                "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML",
            })
        except Exception:
            logger.info("Telegram exit notification failed (non-critical)")

    elif clob_status in ("CANCELLED", "UNMATCHED"):
        logger.warning("Position %d (%s): exit SELL %s — reverting to filled, will retry", position_id, slug, clob_status)
        await mark_exit_failed(pool, position_id)

        try:
            from trading.telegram_confirm import _post  # noqa: PLC0415
            msg = (
                f"⚠️ <b>Exit Order Cancelled</b>\n"
                f"Market: {slug}\n"
                f"SELL <code>{sell_order_id[:20]}</code> was {clob_status.lower()}.\n"
                f"Position still held — will retry on next stop-loss tick."
            )
            await _post(tg_token, "sendMessage", {
                "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML",
            })
        except Exception:
            pass

    # LIVE → wait, no action this cycle


async def poll_order_fills(
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    tg_token: str,
    tg_chat_id: str,
    config: dict,
) -> None:
    """Background task: poll CLOB for order fill status every 60 seconds."""
    interval = config.get("trading", {}).get("order_poll_interval_sec", _POLL_INTERVAL_SEC)
    # One-time schema migration
    try:
        await _ensure_fill_status_column(pool)
        logger.info("Order poller started (interval=%ds)", interval)
    except Exception as exc:
        logger.error("Order poller init failed: %s", exc)
        return

    while True:
        try:
            positions = await get_open_positions(pool)
            if positions:
                logger.info("Order poller: checking %d open position(s)", len(positions))

            for pos in positions:
                order_id = pos["clob_order_id"] or ""
                fill_status = (pos["fill_status"] or "pending").lower()

                # T-35: positions with a pending exit SELL get their own branch.
                # The buy-side order_id is irrelevant once we're in exit_pending.
                if fill_status == "exit_pending":
                    await _handle_exit_pending(pos, pool, executor, tg_token, tg_chat_id)
                    continue

                if not order_id:
                    await _handle_no_order_id(pos, pool, executor, tg_token, tg_chat_id)
                elif order_id.startswith("DRY_"):
                    # T-47: auto-fill DRY_ orders on first poll so dry_run paper
                    # positions behave like real filled positions for the rest
                    # of the lifecycle (risk_guard stop-loss/take-profit,
                    # auto-exit before game, exit_pending DRY_SELL flow).
                    # Previously: `pass` left them in fill_status='pending'
                    # forever, risk_guard skipped them (only monitors 'filled'),
                    # and auto-exit never fired → paper trading was "decorative":
                    # positions opened but never closed, no simulated P&L.
                    if (pos.get("fill_status") or "pending").lower() == "pending":
                        await _set_fill_status(pool, pos["id"], "filled")
                        logger.info(
                            "Position %d (%s): DRY_ order auto-filled (dry_run)",
                            pos["id"], pos["slug"] or pos["market_id"],
                        )
                else:
                    await _handle_existing_order(pos, pool, executor, tg_token, tg_chat_id, config)

        except asyncio.CancelledError:
            logger.info("Order poller cancelled.")
            return
        except Exception as exc:
            logger.error("Order poller cycle error: %s", exc)

        await asyncio.sleep(interval)
