"""
Exit Monitor — auto-closes open positions.

Two triggers:
  1. Time-based (check_and_exit): sell when game_start is within N minutes.
  2. Stagnation (check_stagnation_exit): sell when price hasn't moved > 0.5¢
     across the last 3 consecutive snapshots — signal has played out.

Called every scan cycle from bot_main.
"""

from __future__ import annotations

import logging
from datetime import timezone

import asyncpg

from trading.clob_executor import ClobExecutor
from trading.position_manager import (
    close_position,
    get_positions_near_expiry,
    log_order,
)
from trading.risk_guard import bid_looks_orphan  # T-49: shared quote sanity guard
from trading.telegram_confirm import send_error_alert, send_exit_notification

logger = logging.getLogger(__name__)


async def check_and_exit(
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
) -> None:
    """Find positions expiring soon and auto-sell them."""
    minutes = config["trading"].get("exit_minutes_before", 30)
    positions = await get_positions_near_expiry(pool, minutes)

    if not positions:
        return

    logger.info("Exit monitor: %d position(s) to close", len(positions))

    for pos in positions:
        position_id = pos["id"]
        market_id = pos["market_id"]
        slug = pos["slug"] or market_id
        token_id = pos["token_id"] or ""
        size_shares = float(pos["size_shares"] or 0)

        if not token_id:
            logger.warning("Position %d has no token_id — cannot sell", position_id)
            await send_error_alert(
                tg_token, tg_chat_id,
                f"Cannot auto-exit {slug}: token_id missing. Close manually."
            )
            continue

        try:
            # T-51: auto-exit cannot skip (game about to start) but we must
            # avoid selling at a dust bid on a thin pre-game book. Unlike
            # risk_guard (T-48) and stagnation (T-49) which skip when orphan
            # is detected, here we fall back to entry_price — "break-even
            # close" — because closing at $0.01 on an otherwise 50¢ market
            # books a phantom 95% loss that never happened. Entry_price is
            # the most honest "we don't know the real exit" fallback: better
            # to record a wash trade than manufacture fake P&L.
            entry_price = float(pos["entry_price"] or 0.01)
            try:
                info = await executor.get_market_info(token_id)
                best_bid = float(info.get("bid") or 0.0)
                ask = float(info.get("ask") or 0.0)
            except Exception:
                best_bid = await executor.get_best_bid(token_id)
                ask = 0.0

            fallback_reason = None
            if best_bid <= 0:
                best_bid = entry_price
                fallback_reason = "no bid on book"
            elif bid_looks_orphan(best_bid, ask, entry_price):
                logger.warning(
                    "Position %d (%s): auto-exit via entry_price fallback — "
                    "orphan dust quote at game time (bid=%.3f ask=%.3f entry=%.3f)",
                    position_id, slug, best_bid, ask, entry_price,
                )
                best_bid = entry_price
                fallback_reason = "orphan dust bid"

            if fallback_reason:
                logger.info("Position %d: using entry_price %.4f (%s)",
                            position_id, best_bid, fallback_reason)

            # Place sell order
            sell_result = await executor.sell(token_id, best_bid, size_shares)
            await log_order(pool, market_id, position_id, "sell", sell_result)

            # Update DB
            pnl_usd = await close_position(pool, position_id, best_bid)

            # Obsidian trade diary (fire-and-forget, non-critical)
            try:
                from pathlib import Path as _Path

                import yaml

                from analytics.obsidian_reporter import log_closed_trade
                _cfg = yaml.safe_load((_Path(__file__).parent.parent / "config" / "settings.yaml").read_text())
                await log_closed_trade(_cfg, position_id)
            except Exception as _exc:
                logger.debug("Obsidian log skipped: %s", _exc)

            # Notify
            await send_exit_notification(
                tg_token, tg_chat_id,
                slug=slug,
                exit_price=best_bid,
                size_shares=size_shares,
                pnl_usd=pnl_usd,
            )

        except Exception as exc:
            logger.error("Auto-exit failed for position %d (%s): %s", position_id, slug, exc)
            await send_error_alert(
                tg_token, tg_chat_id,
                f"Auto-exit FAILED for {slug}: {exc}\nClose manually on Polymarket!"
            )


async def check_stagnation_exit(
    pool: asyncpg.Pool,
    executor: ClobExecutor,
    config: dict,
    tg_token: str,
    tg_chat_id: str,
    n_flat: int = 3,
    min_move: float = 0.005,
) -> None:
    """Exit positions where price hasn't moved > 0.5¢ across last N snapshots.

    Guards:
    - Position must be held ≥ 30 min (enough snapshots to evaluate)
    - Game must be > 2h away (closer: time-based exit handles it)
    - Need at least n_flat snapshots since entry_ts
    """
    min_hours_held = config["trading"].get("stagnation_min_hours_held", 0.5)
    max_hours_to_game = config["trading"].get("stagnation_max_hours_to_game", 2.0)

    positions = await pool.fetch(
        """
        SELECT id, market_id, slug, token_id, size_shares, entry_price,
               entry_ts, game_start
        FROM open_positions
        WHERE status = 'open'
          AND fill_status = 'filled'
          AND entry_ts < NOW() - ($1 * INTERVAL '1 hour')
          AND (game_start IS NULL OR game_start > NOW() + ($2 * INTERVAL '1 hour'))
        """,
        min_hours_held,
        max_hours_to_game,
    )

    if not positions:
        return

    for pos in positions:
        position_id = pos["id"]
        market_id = pos["market_id"]
        slug = pos["slug"] or market_id
        entry_ts = pos["entry_ts"]
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=timezone.utc)

        # Fetch last n_flat snapshots since entry
        rows = await pool.fetch(
            """
            SELECT mid_price FROM price_snapshots
            WHERE market_id = $1
              AND ts >= $2
            ORDER BY ts DESC
            LIMIT $3
            """,
            market_id, entry_ts, n_flat,
        )

        if len(rows) < n_flat:
            continue  # not enough data yet

        prices = [float(r["mid_price"]) for r in rows]
        moves = [abs(prices[i] - prices[i + 1]) for i in range(len(prices) - 1)]

        if any(m >= min_move for m in moves):
            continue  # price still moving — hold

        # All moves < min_move → stagnation detected
        logger.info(
            "Stagnation exit: position %d (%s)  last %d moves: %s  (threshold %.4f)",
            position_id, slug, n_flat - 1,
            [f"{m:.4f}" for m in moves], min_move,
        )

        token_id = pos["token_id"] or ""
        size_shares = float(pos["size_shares"] or 0)

        if not token_id:
            logger.warning("Stagnation exit: position %d has no token_id", position_id)
            continue

        try:
            # T-49: use market_info (bid + ask) so we can apply the same
            # orphan-dust-quote guard that risk_guard uses. Without it,
            # stagnation exits close at bid=0.01 on thin pre-game books
            # (dead ask at 0.99 → mid stays 0.5 → stagnation fires → we
            # tried to SELL at the dust bid and booked a fake -95% loss).
            # See risk_guard.bid_looks_orphan for the full rationale.
            try:
                info = await executor.get_market_info(token_id)
                best_bid = float(info.get("bid") or 0.0)
                ask = float(info.get("ask") or 0.0)
            except Exception:
                best_bid = await executor.get_best_bid(token_id)
                ask = 0.0

            entry_price = float(pos["entry_price"] or 0)
            if best_bid > 0 and bid_looks_orphan(best_bid, ask, entry_price):
                logger.warning(
                    "%s: stagnation exit skipped — orphan dust quote "
                    "(bid=%.3f ask=%.3f entry=%.3f). Will retry when book thickens "
                    "or auto-exit-before-game takes over.",
                    slug, best_bid, ask, entry_price,
                )
                continue

            if best_bid <= 0:
                best_bid = float(pos["entry_price"] or 0.01)

            sell_result = await executor.sell(token_id, best_bid, size_shares)
            await log_order(pool, market_id, position_id, "sell", sell_result)
            pnl_usd = await close_position(pool, position_id, best_bid)

            await send_exit_notification(
                tg_token, tg_chat_id,
                slug=slug,
                exit_price=best_bid,
                size_shares=size_shares,
                pnl_usd=pnl_usd,
            )
            logger.info(
                "Stagnation exit complete: %s @ %.4f  P&L: %+.2f USD",
                slug, best_bid, pnl_usd,
            )

        except Exception as exc:
            logger.error("Stagnation exit failed for %d (%s): %s", position_id, slug, exc)
            await send_error_alert(
                tg_token, tg_chat_id,
                f"Stagnation exit FAILED for {slug}: {exc}"
            )
