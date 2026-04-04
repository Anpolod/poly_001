-- Polymarket Sports — Database Schema
-- PostgreSQL + TimescaleDB

-- Ринки (метадані)
CREATE TABLE IF NOT EXISTS markets (
    id              TEXT PRIMARY KEY,
    slug            TEXT,
    question        TEXT,
    sport           TEXT NOT NULL,
    league          TEXT NOT NULL,
    event_start     TIMESTAMPTZ NOT NULL,
    token_id_yes    TEXT,
    token_id_no     TEXT,
    status          TEXT DEFAULT 'active',  -- active / settled / cancelled
    fee_rate_yes    NUMERIC(8,6),
    fee_rate_no     NUMERIC(8,6),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_markets_sport ON markets(sport);
CREATE INDEX IF NOT EXISTS idx_markets_league ON markets(league);
CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status);
CREATE INDEX IF NOT EXISTS idx_markets_event_start ON markets(event_start);

-- Фаза 0: результати cost analysis
CREATE TABLE IF NOT EXISTS cost_analysis (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT REFERENCES markets(id),
    scanned_at      TIMESTAMPTZ DEFAULT NOW(),
    best_bid        NUMERIC(8,4),
    best_ask        NUMERIC(8,4),
    spread          NUMERIC(8,4),
    spread_pct      NUMERIC(8,4),
    bid_depth       NUMERIC(12,2),
    ask_depth       NUMERIC(12,2),
    volume_24h      NUMERIC(12,2),
    fee_rate        NUMERIC(8,6),
    taker_rt_cost   NUMERIC(8,4),  -- round-trip cost як taker (%)
    maker_rt_cost   NUMERIC(8,4),  -- round-trip cost як maker (%)
    move_1h         NUMERIC(8,4),
    move_6h         NUMERIC(8,4),
    move_24h        NUMERIC(8,4),
    move_48h        NUMERIC(8,4),
    move_72h        NUMERIC(8,4),
    ratio_24h       NUMERIC(8,4),  -- move_24h / taker_rt_cost
    ratio_48h       NUMERIC(8,4),
    verdict         TEXT  -- GO / MARGINAL / NO_GO
);

-- Снапшоти цін (time-series, Фаза 1)
CREATE TABLE IF NOT EXISTS price_snapshots (
    ts              TIMESTAMPTZ NOT NULL,
    market_id       TEXT NOT NULL,
    best_bid        NUMERIC(8,4),
    best_ask        NUMERIC(8,4),
    mid_price       NUMERIC(8,4),
    spread          NUMERIC(8,4),
    bid_depth       NUMERIC(12,2),
    ask_depth       NUMERIC(12,2),
    volume_24h      NUMERIC(12,2),
    time_to_event_h NUMERIC(8,2),
    PRIMARY KEY (ts, market_id)
);

-- TimescaleDB hypertable
SELECT create_hypertable('price_snapshots', 'ts', if_not_exists => TRUE);

-- Трейди (Фаза 1)
CREATE TABLE IF NOT EXISTS trades (
    ts              TIMESTAMPTZ NOT NULL,
    market_id       TEXT NOT NULL,
    trade_id        TEXT,
    price           NUMERIC(8,4),
    size            NUMERIC(12,2),
    side            TEXT,  -- buy / sell
    PRIMARY KEY (ts, market_id, trade_id)
);

SELECT create_hypertable('trades', 'ts', if_not_exists => TRUE);

-- Індекси для аналітики
CREATE INDEX IF NOT EXISTS idx_snapshots_market ON price_snapshots(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id, ts);

-- Real-time spike events detected by SpikeTracker in ws_client
CREATE TABLE IF NOT EXISTS spike_events (
    id             BIGSERIAL PRIMARY KEY,
    market_id      TEXT NOT NULL,
    start_ts       TIMESTAMPTZ NOT NULL,
    peak_ts        TIMESTAMPTZ,
    end_ts         TIMESTAMPTZ,
    start_price    NUMERIC(8,4),
    peak_price     NUMERIC(8,4),
    end_price      NUMERIC(8,4),
    magnitude      NUMERIC(8,4),        -- abs(peak - start), price units
    direction      TEXT,                 -- 'up' / 'down'
    n_steps        INTEGER,
    post_1h_price  NUMERIC(8,4),        -- filled by scheduled backfill job
    post_2h_price  NUMERIC(8,4),
    reversion_pct  NUMERIC(6,4),        -- (peak - post_2h) / magnitude
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_spike_events_market ON spike_events(market_id, start_ts);

-- Cost estimates computed from live snapshots (for markets not in cost_analysis)
CREATE TABLE IF NOT EXISTS cost_estimates (
    market_id     TEXT PRIMARY KEY REFERENCES markets(id),
    computed_at   TIMESTAMPTZ DEFAULT NOW(),
    best_bid      NUMERIC(8,4),
    best_ask      NUMERIC(8,4),
    spread        NUMERIC(8,4),
    spread_pct    NUMERIC(8,4),
    taker_rt_cost NUMERIC(8,4),
    maker_rt_cost NUMERIC(8,4),
    source        TEXT DEFAULT 'computed'  -- 'computed' or 'manual' (from phase0 CSV)
);

-- Gaps tracking (пропуски даних)
CREATE TABLE IF NOT EXISTS data_gaps (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL,
    gap_start       TIMESTAMPTZ NOT NULL,
    gap_end         TIMESTAMPTZ,
    gap_minutes     NUMERIC(8,2),
    reason          TEXT  -- ws_disconnect / api_error / unknown
);
