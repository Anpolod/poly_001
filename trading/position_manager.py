"""
Position Manager — async DB CRUD for open_positions and order_log.

Follows the same asyncpg pool pattern as db/repository.py.
All methods accept the pool as first argument and run idempotent queries.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


async def open_position(
    pool: asyncpg.Pool,
    market_id: str,
    slug: str,
    signal_type: str,
    token_id: str,
    size_usd: float,
    size_shares: float,
    entry_price: float,
    game_start: Optional[datetime],
    clob_order_id: str,
    notes: str = "",
    side: str = "YES",
) -> int:
    """Insert a new open position. Returns the new row id.

    T-38: `side` defaults to 'YES' for backward compat (tanking/prop always buy
    YES). The MLB pitcher path passes the resolved `favored_side` so NO-side
    trades are persisted accurately — otherwise the DB ledger misrepresents
    half of MLB trades and dashboards/operator review get the wrong side.
    """
    row = await pool.fetchrow(
        """
        INSERT INTO open_positions
            (market_id, slug, signal_type, side, token_id,
             size_usd, size_shares, entry_price, game_start,
             clob_order_id, notes)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        RETURNING id
        """,
        market_id, slug, signal_type, side, token_id,
        size_usd, size_shares, entry_price, game_start,
        clob_order_id, notes,
    )
    position_id = row["id"]
    logger.info(
        "Opened position %d: %s %s-side  %.2f shares @ %.4f",
        position_id, slug, side, size_shares, entry_price,
    )
    return position_id


async def close_position(
    pool: asyncpg.Pool,
    position_id: int,
    exit_price: float,
) -> float:
    """Mark position as closed, compute P&L. Returns pnl_usd."""
    row = await pool.fetchrow(
        "SELECT size_shares, entry_price FROM open_positions WHERE id=$1",
        position_id,
    )
    if row is None:
        logger.warning("close_position: position %d not found", position_id)
        return 0.0

    size_shares = float(row["size_shares"])
    entry_price = float(row["entry_price"])
    pnl_usd = round((exit_price - entry_price) * size_shares, 4)

    await pool.execute(
        """
        UPDATE open_positions
        SET status='closed', exit_price=$1, exit_ts=NOW(), pnl_usd=$2
        WHERE id=$3
        """,
        exit_price, pnl_usd, position_id,
    )
    logger.info("Closed position %d @ %.4f  P&L: %+.2f USD", position_id, exit_price, pnl_usd)
    return pnl_usd


async def get_open_positions(pool: asyncpg.Pool) -> list[dict]:
    """Return all rows with status='open' as plain dicts.

    T-37 — rows are normalised to dict at fetch time. asyncpg.Record does NOT
    support `.get(key, default)`, only subscripting, so every downstream caller
    that uses `.get()` would AttributeError. Returning dicts makes `.get()`
    idiomatic and keeps unit-test mocks (which use dict) honest.
    """
    rows = await pool.fetch(
        "SELECT * FROM open_positions WHERE status='open' ORDER BY entry_ts"
    )
    return [dict(r) for r in rows]


async def get_total_exposure(pool: asyncpg.Pool) -> float:
    """Return sum of size_usd across all open positions."""
    row = await pool.fetchrow(
        "SELECT COALESCE(SUM(size_usd), 0) AS total FROM open_positions WHERE status='open'"
    )
    return float(row["total"])


async def has_position(pool: asyncpg.Pool, market_id: str) -> bool:
    """Return True if there is already an open position for this market."""
    row = await pool.fetchrow(
        "SELECT 1 FROM open_positions WHERE market_id=$1 AND status='open'",
        market_id,
    )
    return row is not None


async def log_order(
    pool: asyncpg.Pool,
    market_id: str,
    position_id: Optional[int],
    action: str,
    order_result: dict,
) -> None:
    """Append a row to order_log for full audit trail."""
    raw = json.dumps(order_result.get("raw") or {}, default=str)
    await pool.execute(
        """
        INSERT INTO order_log
            (market_id, position_id, action, clob_order_id, price, size_usd, status, raw_response)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """,
        market_id,
        position_id,
        action,
        order_result.get("order_id") or "",
        order_result.get("price"),
        order_result.get("size_usd"),
        order_result.get("status") or "unknown",
        raw,
    )


async def update_clob_order_id(
    pool: asyncpg.Pool,
    position_id: int,
    clob_order_id: str,
) -> None:
    """Update the clob_order_id for a position (e.g. after a retry)."""
    await pool.execute(
        "UPDATE open_positions SET clob_order_id=$1 WHERE id=$2",
        clob_order_id, position_id,
    )


async def mark_position_cancelled(pool: asyncpg.Pool, position_id: int) -> None:
    """Mark a position as cancelled (order rejected / never filled)."""
    await pool.execute(
        "UPDATE open_positions SET status='cancelled', exit_ts=NOW() WHERE id=$1",
        position_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# T-35 — Exit-flow helpers (mark_exit_pending / mark_exit_failed)
# ──────────────────────────────────────────────────────────────────────────────


async def ensure_exit_order_id_column(pool: asyncpg.Pool) -> None:
    """One-time migration: add exit_order_id column if missing.

    Called by stop_loss_monitor on startup, mirroring the existing
    current_bid migration pattern. Safe to call repeatedly.
    """
    await pool.execute(
        "ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS exit_order_id TEXT"
    )


async def mark_exit_pending(
    pool: asyncpg.Pool,
    position_id: int,
    exit_order_id: str,
    reason: str = "",
) -> None:
    """Mark a position as awaiting an exit fill.

    Called by risk_guard after placing a stop-loss / take-profit SELL.
    The order_poller finalizes the close once the SELL is MATCHED on CLOB.
    Reverts to fill_status='filled' if the SELL is CANCELLED/UNMATCHED.
    """
    await pool.execute(
        """
        UPDATE open_positions
        SET fill_status='exit_pending',
            exit_order_id=$1,
            notes=COALESCE(notes,'') || $2
        WHERE id=$3
        """,
        exit_order_id,
        f" [exit_pending: {reason}]" if reason else " [exit_pending]",
        position_id,
    )
    logger.info("Position %d → exit_pending (sell order %s, reason=%s)",
                position_id, exit_order_id[:16] if exit_order_id else "?", reason)


async def mark_exit_failed(pool: asyncpg.Pool, position_id: int) -> None:
    """Revert from exit_pending → filled (the SELL was cancelled / never filled).

    Position is still held; the next stop-loss tick will retry the exit at the
    then-current bid. exit_order_id is cleared so the poller stops watching it.
    """
    await pool.execute(
        """
        UPDATE open_positions
        SET fill_status='filled',
            exit_order_id=NULL,
            notes=COALESCE(notes,'') || ' [exit_failed:revert]'
        WHERE id=$1
        """,
        position_id,
    )
    logger.warning("Position %d → reverted exit_pending → filled (sell never matched)",
                   position_id)


# ──────────────────────────────────────────────────────────────────────────────
# T-35 — YES/NO side resolver for binary moneyline markets
# ──────────────────────────────────────────────────────────────────────────────


def _resolve_yes_no_teams_from_text(
    text: str,
    aliases: dict[str, str],
) -> tuple[Optional[str], Optional[str]]:
    """Parse a Polymarket binary market question (or slug) to determine YES vs NO.

    Polymarket convention: the FIRST team mentioned in the question is the YES
    side ("Warriors vs. Clippers" → YES=Warriors, NO=Clippers).

    This is position-based unlike `match_teams_in_question` which sorts by alias
    length. We still iterate longest-alias-first so "los angeles lakers" wins
    over "lakers" and "hornets" wins over "nets" — but the final ordering is by
    where each canonical team appears in the text.

    Returns (yes_canonical, no_canonical). Either may be None on parse failure.
    """
    if not text:
        return None, None
    text_lower = text.lower()

    # T-54: track the character spans of already-matched aliases so that a
    # shorter alias can't match INSIDE a longer alias we already took.
    # Length-descending iteration alone does not prevent this:
    # for "Spread: Hornets (-3.5)", "hornets" matches at pos 8-15, and then
    # "nets" matches at pos 11-15 INSIDE that same word. Without the span
    # guard we'd report a Hornets-vs-Nets market that never existed — which
    # is exactly what happened to all 7 tanking positions (market 1999250)
    # before this fix.
    matches: list[tuple[int, str]] = []  # (position_in_text, canonical_name)
    seen_canonical: set[str] = set()
    used_spans: list[tuple[int, int]] = []
    for alias in sorted(aliases.keys(), key=len, reverse=True):
        canonical = aliases[alias]
        if canonical in seen_canonical:
            continue
        # Scan ALL occurrences of `alias` and pick the first one that doesn't
        # overlap with any already-claimed span. For "Hornets vs. Nets",
        # `alias="nets"` finds "nets" first INSIDE "Hornets" (overlapping
        # the Charlotte Hornets claim) AND later as its own word — we must
        # keep searching past the overlapping hit to find the second one.
        start_search = 0
        clean_pos = -1
        while True:
            pos = text_lower.find(alias, start_search)
            if pos < 0:
                break
            end = pos + len(alias)
            if any(not (end <= s or pos >= e) for s, e in used_spans):
                start_search = pos + 1   # try the next occurrence
                continue
            clean_pos = pos
            break
        if clean_pos < 0:
            continue
        end = clean_pos + len(alias)
        matches.append((clean_pos, canonical))
        seen_canonical.add(canonical)
        used_spans.append((clean_pos, end))
        if len(matches) == 2:
            break

    if len(matches) < 2:
        return None, None

    matches.sort(key=lambda m: m[0])
    return matches[0][1], matches[1][1]


async def resolve_team_token_side(
    pool: asyncpg.Pool,
    market_id: str,
    target_team: str,
    aliases: dict[str, str],
) -> tuple[Optional[str], Optional[str]]:
    """Return (token_id_to_buy, side: 'YES'|'NO') for `target_team` in this market.

    Tries the question first (full team names), falls back to the slug
    (still uses the same alias map). Returns (None, None) if the market is
    missing, the question can't be parsed, or `target_team` doesn't match
    either side.

    Caller pattern:
        token_id, side = await resolve_team_token_side(pool, mid, "Boston Celtics", nba_aliases)
        if side is None:
            # fall back to alert-only / skip trade
        elif side == "YES":
            price_for_team = market_yes_mid_price
        else:
            price_for_team = 1.0 - market_yes_mid_price
    """
    row = await pool.fetchrow(
        "SELECT slug, question, token_id_yes, token_id_no FROM markets WHERE id=$1",
        market_id,
    )
    if row is None:
        return None, None

    yes_token = row["token_id_yes"] or ""
    no_token = row["token_id_no"] or ""
    if not yes_token or not no_token:
        return None, None

    # Try question first — usually carries full team names ("Warriors vs. Clippers")
    yes_team, no_team = _resolve_yes_no_teams_from_text(row["question"] or "", aliases)
    if yes_team is None:
        # Slug fallback ("nba-warriors-clippers-2026-04-15") — only works if alias
        # dict contains slug-friendly substrings. Nicer for sports without
        # human-readable questions.
        yes_team, no_team = _resolve_yes_no_teams_from_text(row["slug"] or "", aliases)

    if yes_team is None:
        return None, None

    if target_team == yes_team:
        return yes_token, "YES"
    if target_team == no_team:
        return no_token, "NO"

    # target_team is neither — caller passed a team that isn't in this market
    return None, None


async def get_positions_near_expiry(
    pool: asyncpg.Pool,
    minutes_before: int,
) -> list[asyncpg.Record]:
    """Return open positions whose game_start is within the next `minutes_before` minutes."""
    return await pool.fetch(
        """
        SELECT * FROM open_positions
        WHERE status = 'open'
          AND game_start IS NOT NULL
          AND game_start < NOW() + ($1 * INTERVAL '1 minute')
        ORDER BY game_start
        """,
        minutes_before,
    )
