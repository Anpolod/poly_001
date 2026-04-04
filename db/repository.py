"""Repository — всі запити до бази даних"""

import asyncpg
import os
from datetime import datetime
from typing import Optional


class Repository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def create(cls, config: dict) -> "Repository":
        db = config["database"]
        password = os.environ.get("DB_PASSWORD") or db["password"]
        pool = await asyncpg.create_pool(
            host=os.environ.get("DB_HOST") or db["host"],
            port=int(os.environ.get("DB_PORT") or db["port"]),
            database=os.environ.get("DB_NAME") or db["name"],
            user=os.environ.get("DB_USER") or db["user"],
            password=password,
            min_size=2,
            max_size=10,
        )
        return cls(pool)

    async def close(self):
        await self.pool.close()

    # --- Markets ---

    async def upsert_market(self, market: dict):
        await self.pool.execute(
            """
            INSERT INTO markets (id, slug, question, sport, league, event_start,
                                 token_id_yes, token_id_no, status, fee_rate_yes, fee_rate_no, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                event_start = EXCLUDED.event_start,
                fee_rate_yes = EXCLUDED.fee_rate_yes,
                fee_rate_no = EXCLUDED.fee_rate_no,
                updated_at = NOW()
            """,
            market["id"],
            market.get("slug"),
            market.get("question"),
            market["sport"],
            market["league"],
            market["event_start"],
            market.get("token_id_yes"),
            market.get("token_id_no"),
            market.get("status", "active"),
            market.get("fee_rate_yes"),
            market.get("fee_rate_no"),
        )

    async def get_active_markets(self) -> list:
        return await self.pool.fetch(
            """
            SELECT * FROM markets
            WHERE status = 'active' AND event_start > NOW()
            ORDER BY event_start
            """
        )

    async def update_market_status(self, market_id: str, status: str):
        await self.pool.execute(
            "UPDATE markets SET status = $1, updated_at = NOW() WHERE id = $2",
            status,
            market_id,
        )

    # --- Cost Analysis (Фаза 0) ---

    async def insert_cost_analysis(self, row: dict):
        await self.pool.execute(
            """
            INSERT INTO cost_analysis (
                market_id, best_bid, best_ask, spread, spread_pct,
                bid_depth, ask_depth, volume_24h, fee_rate,
                taker_rt_cost, maker_rt_cost,
                move_1h, move_6h, move_24h, move_48h, move_72h,
                ratio_24h, ratio_48h, verdict
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
            """,
            row["market_id"],
            row.get("best_bid"),
            row.get("best_ask"),
            row.get("spread"),
            row.get("spread_pct"),
            row.get("bid_depth"),
            row.get("ask_depth"),
            row.get("volume_24h"),
            row.get("fee_rate"),
            row.get("taker_rt_cost"),
            row.get("maker_rt_cost"),
            row.get("move_1h"),
            row.get("move_6h"),
            row.get("move_24h"),
            row.get("move_48h"),
            row.get("move_72h"),
            row.get("ratio_24h"),
            row.get("ratio_48h"),
            row.get("verdict"),
        )

    # --- Price Snapshots (Фаза 1) ---

    async def insert_snapshot(self, snap: dict):
        await self.pool.execute(
            """
            INSERT INTO price_snapshots (
                ts, market_id, best_bid, best_ask, mid_price,
                spread, bid_depth, ask_depth, volume_24h, time_to_event_h
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (ts, market_id) DO NOTHING
            """,
            snap["ts"],
            snap["market_id"],
            snap.get("best_bid"),
            snap.get("best_ask"),
            snap.get("mid_price"),
            snap.get("spread"),
            snap.get("bid_depth"),
            snap.get("ask_depth"),
            snap.get("volume_24h"),
            snap.get("time_to_event_h"),
        )

    # --- Trades (Фаза 1) ---

    async def insert_trade(self, trade: dict):
        await self.pool.execute(
            """
            INSERT INTO trades (ts, market_id, trade_id, price, size, side)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (ts, market_id, trade_id) DO NOTHING
            """,
            trade["ts"],
            trade["market_id"],
            trade.get("trade_id", ""),
            trade.get("price"),
            trade.get("size"),
            trade.get("side"),
        )

    # --- Spike Events ---

    async def insert_spike_event(self, event: dict):
        await self.pool.execute(
            """
            INSERT INTO spike_events
                (market_id, start_ts, peak_ts, end_ts,
                 start_price, peak_price, end_price,
                 magnitude, direction, n_steps)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            event["market_id"],
            event["start_ts"],
            event.get("peak_ts"),
            event.get("end_ts"),
            event.get("start_price"),
            event.get("peak_price"),
            event.get("end_price"),
            event.get("magnitude"),
            event.get("direction"),
            event.get("n_steps"),
        )

    # --- Data Gaps ---

    async def insert_gap(self, market_id: str, gap_start: datetime, reason: str):
        await self.pool.execute(
            """
            INSERT INTO data_gaps (market_id, gap_start, reason)
            VALUES ($1, $2, $3)
            """,
            market_id,
            gap_start,
            reason,
        )

    async def close_gap(self, market_id: str, gap_start: datetime, gap_end: datetime):
        minutes = (gap_end - gap_start).total_seconds() / 60
        await self.pool.execute(
            """
            UPDATE data_gaps SET gap_end = $1, gap_minutes = $2
            WHERE market_id = $3 AND gap_start = $4 AND gap_end IS NULL
            """,
            gap_end,
            minutes,
            market_id,
            gap_start,
        )
