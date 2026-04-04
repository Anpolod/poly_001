"""
Parameterized async query functions for the Polymarket Sports TimescaleDB schema.

All functions accept an asyncpg connection object as the first argument.
All timestamps are stored and returned in UTC.
No string formatting is used for query construction — all user-supplied values
are passed as positional parameters ($1, $2, …) to prevent SQL injection.
"""

from __future__ import annotations

from typing import Optional


async def insert_snapshot(conn, snapshot: dict) -> None:
    """Insert a single price snapshot row into price_snapshots.

    Silently skips duplicate rows (same ts + market_id primary key) via
    ON CONFLICT DO NOTHING, making the function safe to call on re-delivery
    of the same data point.

    Args:
        conn:     An asyncpg connection (or pool-acquired connection).
        snapshot: Dict with keys matching price_snapshots columns:
                    ts, market_id, best_bid, best_ask, mid_price, spread,
                    bid_depth, ask_depth, volume_24h, time_to_event_h.
    """
    await conn.execute(
        """
        INSERT INTO price_snapshots (
            ts,
            market_id,
            best_bid,
            best_ask,
            mid_price,
            spread,
            bid_depth,
            ask_depth,
            volume_24h,
            time_to_event_h
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10
        )
        ON CONFLICT DO NOTHING
        """,
        snapshot["ts"],
        snapshot["market_id"],
        snapshot["best_bid"],
        snapshot["best_ask"],
        snapshot["mid_price"],
        snapshot["spread"],
        snapshot["bid_depth"],
        snapshot["ask_depth"],
        snapshot["volume_24h"],
        snapshot["time_to_event_h"],
    )


async def insert_market(conn, market: dict) -> None:
    """Upsert a market row into the markets table.

    On conflict (same primary key id), updates the mutable fields that change
    over the market's lifetime: status, event_start, and updated_at.
    Immutable metadata fields (slug, question, sport, league, token IDs,
    fee rates, created_at) are left unchanged on conflict.

    Args:
        conn:   An asyncpg connection.
        market: Dict with keys matching markets columns. Required keys:
                  id, sport, league, event_start.
                Optional keys (None if absent):
                  slug, question, token_id_yes, token_id_no, status,
                  fee_rate_yes, fee_rate_no, updated_at.
    """
    await conn.execute(
        """
        INSERT INTO markets (
            id,
            slug,
            question,
            sport,
            league,
            event_start,
            token_id_yes,
            token_id_no,
            status,
            fee_rate_yes,
            fee_rate_no,
            updated_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8,
            COALESCE($9, 'active'),
            $10, $11,
            COALESCE($12, NOW())
        )
        ON CONFLICT (id) DO UPDATE SET
            status      = EXCLUDED.status,
            event_start = EXCLUDED.event_start,
            updated_at  = EXCLUDED.updated_at
        """,
        market["id"],
        market.get("slug"),
        market.get("question"),
        market["sport"],
        market["league"],
        market["event_start"],
        market.get("token_id_yes"),
        market.get("token_id_no"),
        market.get("status"),
        market.get("fee_rate_yes"),
        market.get("fee_rate_no"),
        market.get("updated_at"),
    )


async def get_market_snapshots(
    conn,
    market_id: str,
    hours_before_event: float,
) -> list[dict]:
    """Return price snapshots for a market within a window before its event.

    Fetches all price_snapshots rows for *market_id* whose timestamp falls
    on or after (event_start - hours_before_event hours), ordered oldest
    first. The event_start is resolved by joining with the markets table.

    Args:
        conn:               An asyncpg connection.
        market_id:          The market's primary-key id string.
        hours_before_event: How many hours before event_start to look back.
                            For example, 48.0 returns the last 48 h of data.

    Returns:
        List of dicts, each representing one price_snapshots row plus the
        market's event_start. Empty list if the market is not found or has
        no snapshots in the window.
    """
    rows = await conn.fetch(
        """
        SELECT
            ps.ts,
            ps.market_id,
            ps.best_bid,
            ps.best_ask,
            ps.mid_price,
            ps.spread,
            ps.bid_depth,
            ps.ask_depth,
            ps.volume_24h,
            ps.time_to_event_h,
            m.event_start
        FROM price_snapshots ps
        JOIN markets m ON m.id = ps.market_id
        WHERE ps.market_id = $1
          AND ps.ts >= m.event_start - ($2 * INTERVAL '1 hour')
        ORDER BY ps.ts ASC
        """,
        market_id,
        hours_before_event,
    )
    return [dict(row) for row in rows]


async def get_active_markets(
    conn,
    sport: Optional[str] = None,
    min_volume: Optional[float] = None,
) -> list[dict]:
    """Return markets that are currently active and have not yet started.

    Optionally filters by sport and/or minimum 24-hour trading volume.
    When min_volume is provided the function joins cost_analysis and picks
    the most recent scan per market via a lateral subquery.

    Args:
        conn:       An asyncpg connection.
        sport:      If provided, restrict results to this sport string.
        min_volume: If provided, only return markets whose latest
                    cost_analysis.volume_24h >= this value.

    Returns:
        List of dicts representing markets rows (plus volume_24h when the
        min_volume filter is active). Ordered by event_start ascending.
    """
    if min_volume is not None:
        rows = await conn.fetch(
            """
            SELECT
                m.*,
                ca.volume_24h
            FROM markets m
            JOIN LATERAL (
                SELECT volume_24h
                FROM cost_analysis
                WHERE market_id = m.id
                ORDER BY scanned_at DESC
                LIMIT 1
            ) ca ON TRUE
            WHERE m.status = 'active'
              AND m.event_start > NOW()
              AND ($1::TEXT IS NULL OR m.sport = $1)
              AND ca.volume_24h >= $2
            ORDER BY m.event_start ASC
            """,
            sport,
            min_volume,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT m.*
            FROM markets m
            WHERE m.status = 'active'
              AND m.event_start > NOW()
              AND ($1::TEXT IS NULL OR m.sport = $1)
            ORDER BY m.event_start ASC
            """,
            sport,
        )
    return [dict(row) for row in rows]


async def get_snapshots_for_analysis(
    conn,
    sport: str,
    league: str,
    min_events: int,
) -> list[dict]:
    """Return all snapshots grouped by market for a sport/league combination.

    Only includes markets that have at least *min_events* snapshot rows,
    making the result set suitable for statistical or ML analysis where a
    minimum sample size per market is required.

    The returned list is flat — each element is one price_snapshots row
    enriched with market metadata (sport, league, question, event_start).
    Rows are ordered by market_id then ts ascending so callers can group
    them with itertools.groupby or pandas groupby without sorting overhead.

    Args:
        conn:       An asyncpg connection.
        sport:      Sport string to filter on (e.g. "soccer", "basketball").
        league:     League string to filter on (e.g. "NBA", "Premier League").
        min_events: Minimum number of snapshot rows a market must have to be
                    included. Markets with fewer snapshots are excluded.

    Returns:
        List of dicts, each with price_snapshots columns plus:
          market_question, market_sport, market_league, event_start.
        Empty list if no qualifying markets are found.
    """
    rows = await conn.fetch(
        """
        SELECT
            ps.ts,
            ps.market_id,
            ps.best_bid,
            ps.best_ask,
            ps.mid_price,
            ps.spread,
            ps.bid_depth,
            ps.ask_depth,
            ps.volume_24h,
            ps.time_to_event_h,
            m.question  AS market_question,
            m.sport     AS market_sport,
            m.league    AS market_league,
            m.event_start
        FROM price_snapshots ps
        JOIN markets m ON m.id = ps.market_id
        WHERE m.sport  = $1
          AND m.league = $2
          AND ps.market_id IN (
              SELECT market_id
              FROM price_snapshots
              WHERE market_id IN (
                  SELECT id FROM markets WHERE sport = $1 AND league = $2
              )
              GROUP BY market_id
              HAVING COUNT(*) >= $3
          )
        ORDER BY ps.market_id ASC, ps.ts ASC
        """,
        sport,
        league,
        min_events,
    )
    return [dict(row) for row in rows]
