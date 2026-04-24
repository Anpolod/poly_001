"""Maker quote/fill ledger — async DB primitives for the T-58 LP rewards MM.

Pure async functions over maker_quotes + maker_fills + inventory queries.
No network, no clob_executor, no scheduler — just CRUD with a clear
lifecycle invariant: LIVE → (CANCELLED | MATCHED | EXPIRED | REJECTED).
The upcoming market_maker agent will orchestrate these plus clob_executor
to place/cancel quotes and detect fills via order_poller.

Design:
  - Quotes are immutable. Cancel/fill stamps status + timestamps, never
    deletes rows. This keeps the full audit trail for weekly reward
    reconciliation and post-mortem debugging.
  - Fills are append-only rows referencing quote_id. A single quote may
    have multiple partial fills. Agent is responsible for calling
    `mark_matched` when cumulative fill_size ≥ quote.size_shares.
  - `inventory_for_market` computes NET position from fills (not quotes),
    which means outstanding LIVE quotes don't count toward inventory
    until they actually fill. This is the correct definition for risk
    limits — a posted quote doesn't tie up the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

import asyncpg


@dataclass
class Quote:
    id: int
    market_id: str
    slug: Optional[str]
    side: str                 # BUY | SELL
    token_id: str
    price: Decimal
    size_shares: Decimal
    clob_order_id: Optional[str]
    placed_at: datetime
    cancelled_at: Optional[datetime]
    status: str               # LIVE | MATCHED | CANCELLED | EXPIRED | REJECTED


async def insert_quote(
    pool: asyncpg.Pool,
    market_id: str,
    side: str,
    token_id: str,
    price: float,
    size_shares: float,
    slug: Optional[str] = None,
    clob_order_id: Optional[str] = None,
    status: str = "LIVE",
) -> int:
    """Record a newly-placed quote. Returns row id."""
    row = await pool.fetchrow(
        """
        INSERT INTO maker_quotes
            (market_id, slug, side, token_id, price, size_shares, clob_order_id, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        market_id, slug, side, token_id, price, size_shares, clob_order_id, status,
    )
    return int(row["id"])


async def cancel_quote(pool: asyncpg.Pool, quote_id: int) -> None:
    """Mark quote CANCELLED. Idempotent: no-op on non-LIVE rows (so a
    late-arriving 'fill detected' race doesn't undo a matched status)."""
    await pool.execute(
        """
        UPDATE maker_quotes
        SET status = 'CANCELLED', cancelled_at = NOW()
        WHERE id = $1 AND status = 'LIVE'
        """,
        quote_id,
    )


async def mark_matched(pool: asyncpg.Pool, quote_id: int) -> None:
    """Mark quote fully filled. Called when accumulated fills cover size_shares."""
    await pool.execute(
        "UPDATE maker_quotes SET status = 'MATCHED' WHERE id = $1",
        quote_id,
    )


async def fetch_live_quotes(
    pool: asyncpg.Pool,
    market_id: Optional[str] = None,
) -> list[Quote]:
    """Return all LIVE quotes, optionally filtered by market_id. Ordered by
    placed_at ASC — oldest first so agent can cancel stale quotes before new."""
    if market_id is None:
        rows = await pool.fetch(
            "SELECT * FROM maker_quotes WHERE status = 'LIVE' ORDER BY placed_at"
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM maker_quotes WHERE status = 'LIVE' AND market_id = $1 "
            "ORDER BY placed_at",
            market_id,
        )
    return [
        Quote(
            id=r["id"],
            market_id=r["market_id"],
            slug=r["slug"],
            side=r["side"],
            token_id=r["token_id"],
            price=r["price"],
            size_shares=r["size_shares"],
            clob_order_id=r["clob_order_id"],
            placed_at=r["placed_at"],
            cancelled_at=r["cancelled_at"],
            status=r["status"],
        )
        for r in rows
    ]


async def record_fill(
    pool: asyncpg.Pool,
    quote_id: int,
    market_id: str,
    side: str,
    fill_price: float,
    fill_size: float,
    clob_trade_id: Optional[str] = None,
) -> int:
    """Record a fill against a quote. Returns fill id. Caller is responsible
    for calling mark_matched(quote_id) when cumulative fill_size ≥
    quote.size_shares — this function DOES NOT cascade the status update
    because partial fills are legitimate (may accumulate over time)."""
    row = await pool.fetchrow(
        """
        INSERT INTO maker_fills
            (quote_id, market_id, side, fill_price, fill_size, clob_trade_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        quote_id, market_id, side, fill_price, fill_size, clob_trade_id,
    )
    return int(row["id"])


async def fill_total_for_quote(pool: asyncpg.Pool, quote_id: int) -> float:
    """Cumulative filled size for one quote. Used by agent to decide
    when to call mark_matched."""
    row = await pool.fetchrow(
        "SELECT COALESCE(SUM(fill_size), 0) AS total FROM maker_fills WHERE quote_id = $1",
        quote_id,
    )
    return float(row["total"] or 0)


async def inventory_for_market(
    pool: asyncpg.Pool,
    market_id: str,
    token_id: str,
) -> float:
    """Net inventory (shares) for this (market_id, token_id). Positive =
    we are long the token. Computed from fills only — outstanding LIVE
    quotes don't tie up inventory until they fill.

    This is the authoritative number the agent's inventory guard checks
    against max_inventory config. Independent of clob_executor — if our
    fills table lags behind CLOB reality, inventory guard may
    underestimate risk. The order_poller is responsible for keeping fills
    current within its poll interval (default 60s)."""
    row = await pool.fetchrow(
        """
        SELECT
          COALESCE(SUM(CASE WHEN mf.side = 'BUY'  THEN mf.fill_size ELSE 0 END), 0) AS buys,
          COALESCE(SUM(CASE WHEN mf.side = 'SELL' THEN mf.fill_size ELSE 0 END), 0) AS sells
        FROM maker_fills mf
        JOIN maker_quotes mq ON mf.quote_id = mq.id
        WHERE mf.market_id = $1 AND mq.token_id = $2
        """,
        market_id, token_id,
    )
    return float((row["buys"] or 0) - (row["sells"] or 0))


async def update_clob_order_id(
    pool: asyncpg.Pool,
    quote_id: int,
    clob_order_id: str,
) -> None:
    """Stamp the CLOB-assigned order id after a successful POST /order.
    The quote was created BEFORE the CLOB call (so we have an id to
    reference even if POST fails); this function fills in the CLOB id
    after the fact. Agent pattern:
        quote_id = await insert_quote(..., clob_order_id=None)
        order = await executor.buy(...)
        if order["order_id"]:
            await update_clob_order_id(pool, quote_id, order["order_id"])
        else:
            await cancel_quote(pool, quote_id)  # mark REJECTED (see below)
    """
    await pool.execute(
        "UPDATE maker_quotes SET clob_order_id = $1 WHERE id = $2",
        clob_order_id, quote_id,
    )


async def mark_rejected(pool: asyncpg.Pool, quote_id: int) -> None:
    """Mark quote REJECTED — CLOB POST failed, so no live order exists.
    Distinct from CANCELLED (we initiated it) and EXPIRED (CLOB timed out)."""
    await pool.execute(
        """
        UPDATE maker_quotes
        SET status = 'REJECTED', cancelled_at = NOW()
        WHERE id = $1
        """,
        quote_id,
    )
