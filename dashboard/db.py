"""
dashboard/db.py — Synchronous PostgreSQL access for the Streamlit dashboard.

Uses psycopg2 (not asyncpg) because Streamlit is a synchronous framework.
Connection pool is cached via @st.cache_resource so it survives reruns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool
import streamlit as st
import yaml


def _load_db_config() -> dict:
    cfg_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    db = cfg["database"]
    return {
        "host": db["host"],
        "port": int(db["port"]),
        "dbname": db["name"],
        "user": db["user"],
        "password": str(db["password"]),
    }


@st.cache_resource
def _pool() -> psycopg2.pool.ThreadedConnectionPool:
    cfg = _load_db_config()
    return psycopg2.pool.ThreadedConnectionPool(1, 5, **cfg)


def _raw_query(sql: str, params: tuple | None = None) -> list[dict]:
    """Low-level: executes SQL against pool, returns list of dicts. Not cached."""
    pool = _pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        pool.putconn(conn)


def query_df(sql: str, params: tuple | None = None) -> list[dict]:
    """Execute SQL and return a list of dicts (one per row)."""
    return _raw_query(sql, params)


def query_one(sql: str, params: tuple | None = None) -> dict | None:
    """Execute SQL and return the first row as a dict, or None."""
    rows = query_df(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | None = None) -> None:
    """Execute a write statement (INSERT/UPDATE). Commits immediately."""
    pool = _pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        pool.putconn(conn)


# Cached wrappers — TTL=5min. Use these for pages, not query_df directly.
@st.cache_data(ttl=300)
def cached_query(sql: str, params: tuple | None = None) -> list[dict]:
    """Cached version of query_df (5-min TTL). Use for all dashboard reads."""
    return _raw_query(sql, params)


# ---------------------------------------------------------------------------
# Canned query helpers (used by multiple pages)
# ---------------------------------------------------------------------------

def market_counts_by_sport() -> list[dict]:
    return query_df("""
        SELECT sport, COUNT(*) AS total,
               SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active
        FROM markets
        GROUP BY sport
        ORDER BY total DESC
    """)


def verdict_counts() -> list[dict]:
    return query_df("""
        SELECT ca.verdict, COUNT(DISTINCT ca.market_id) AS cnt
        FROM cost_analysis ca
        JOIN (
            SELECT market_id, MAX(scanned_at) AS latest
            FROM cost_analysis GROUP BY market_id
        ) latest ON ca.market_id = latest.market_id AND ca.scanned_at = latest.latest
        GROUP BY ca.verdict
        ORDER BY ca.verdict
    """)


def snapshot_rate_last_hour() -> dict | None:
    return query_one("""
        SELECT COUNT(*) AS snapshots,
               COUNT(DISTINCT market_id) AS markets,
               MAX(ts) AS last_ts
        FROM price_snapshots
        WHERE ts > NOW() - INTERVAL '1 hour'
    """)


def top_movers(limit: int = 10) -> list[dict]:
    """Markets with the largest absolute mid_price change in the last 24h."""
    return query_df("""
        WITH recent AS (
            SELECT market_id,
                   FIRST_VALUE(mid_price) OVER w AS price_now,
                   LAST_VALUE(mid_price) OVER w AS price_24h_ago,
                   ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY ts DESC) AS rn
            FROM price_snapshots
            WHERE ts > NOW() - INTERVAL '24 hours'
            WINDOW w AS (PARTITION BY market_id ORDER BY ts
                         ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
        )
        SELECT r.market_id, m.question, m.sport, m.league,
               r.price_now, r.price_24h_ago,
               ABS(r.price_now - r.price_24h_ago) AS move_abs
        FROM recent r
        JOIN markets m ON m.id = r.market_id
        WHERE r.rn = 1
        ORDER BY move_abs DESC NULLS LAST
        LIMIT %s
    """, (limit,))


def markets_with_latest_analysis(sport_filter: str = "", verdict_filter: str = "") -> list[dict]:
    """All markets joined to their most recent cost_analysis row."""
    filters = []
    params: list[Any] = []
    if sport_filter:
        filters.append("m.sport = %s")
        params.append(sport_filter)
    if verdict_filter:
        filters.append("ca.verdict = %s")
        params.append(verdict_filter)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    return query_df(f"""
        SELECT m.id AS market_id, m.slug, m.question, m.sport, m.league,
               m.status, m.event_start,
               ca.verdict, ca.spread_pct, ca.taker_rt_cost, ca.ratio_24h,
               ca.volume_24h, ca.scanned_at AS analysis_ts
        FROM markets m
        LEFT JOIN LATERAL (
            SELECT * FROM cost_analysis
            WHERE market_id = m.id
            ORDER BY scanned_at DESC
            LIMIT 1
        ) ca ON TRUE
        {where}
        ORDER BY m.event_start ASC NULLS LAST
    """, tuple(params) if params else None)


def price_history(market_id: str, hours: int = 48) -> list[dict]:
    return query_df("""
        SELECT ts, mid_price, spread, bid_depth, ask_depth, volume_24h
        FROM price_snapshots
        WHERE market_id = %s AND ts > NOW() - INTERVAL '%s hours'
        ORDER BY ts ASC
    """ % ('%s', hours), (market_id,))


def all_sports() -> list[str]:
    rows = query_df("SELECT DISTINCT sport FROM markets ORDER BY sport")
    return [r["sport"] for r in rows]


def tanking_signals_recent(hours: int = 48) -> list[dict]:
    return query_df("""
        SELECT ts.id, ts.scanned_at, ts.market_id, m.question,
               ts.game_start, ts.motivated_team, ts.tanking_team,
               ts.motivation_differential, ts.current_price, ts.drift_24h,
               ts.pattern_strength, ts.action, ts.lineup_notes
        FROM tanking_signals ts
        LEFT JOIN markets m ON m.id = ts.market_id
        WHERE ts.scanned_at > NOW() - INTERVAL '%s hours'
        ORDER BY ts.scanned_at DESC, ts.motivation_differential DESC
    """ % hours)


def prop_signals_recent(days: int = 7) -> list[dict]:
    return query_df("""
        SELECT player_name, prop_type, threshold, yes_price,
               model_win, roi_pct, hours_until, outcome,
               scanned_at, game_start
        FROM prop_scan_log
        WHERE scanned_at > NOW() - INTERVAL '%s days'
        ORDER BY scanned_at DESC
    """ % days)


def data_gaps_recent(limit: int = 50) -> list[dict]:
    return query_df("""
        SELECT dg.id, dg.market_id, m.question,
               dg.gap_start, dg.gap_end, dg.gap_minutes, dg.reason
        FROM data_gaps dg
        LEFT JOIN markets m ON m.id = dg.market_id
        ORDER BY dg.gap_start DESC
        LIMIT %s
    """, (limit,))


def snapshot_counts_per_market(hours: int = 24) -> list[dict]:
    return query_df("""
        SELECT ps.market_id, m.question, m.sport,
               COUNT(*) AS snapshots,
               MAX(ps.ts) AS last_ts
        FROM price_snapshots ps
        LEFT JOIN markets m ON m.id = ps.market_id
        WHERE ps.ts > NOW() - INTERVAL '%s hours'
        GROUP BY ps.market_id, m.question, m.sport
        ORDER BY snapshots DESC
    """ % hours)
