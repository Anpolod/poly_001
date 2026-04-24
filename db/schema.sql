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

-- TimescaleDB hypertable (skipped gracefully if TimescaleDB not installed)
DO $$ BEGIN
  PERFORM create_hypertable('price_snapshots', 'ts', if_not_exists => TRUE);
EXCEPTION WHEN undefined_function THEN
  RAISE NOTICE 'TimescaleDB not installed — price_snapshots will be a regular table';
END $$;

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

DO $$ BEGIN
  PERFORM create_hypertable('trades', 'ts', if_not_exists => TRUE);
EXCEPTION WHEN undefined_function THEN
  RAISE NOTICE 'TimescaleDB not installed — trades will be a regular table';
END $$;

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

-- Prop scanner signal log (NBA player props pre-match scanner hits)
CREATE TABLE IF NOT EXISTS prop_scan_log (
    id           SERIAL PRIMARY KEY,
    scanned_at   TIMESTAMPTZ DEFAULT NOW(),
    market_id    TEXT NOT NULL,
    slug         TEXT,
    prop_type    TEXT,        -- points | rebounds | assists
    player_name  TEXT,
    threshold    TEXT,        -- e.g. '23.5'
    game_start   TIMESTAMPTZ,
    hours_until  FLOAT,
    yes_price    FLOAT,
    model_win    FLOAT,
    ev_per_unit  FLOAT,
    roi_pct      FLOAT,
    bid_depth    FLOAT,
    ask_depth    FLOAT,
    outcome      SMALLINT,   -- 1 = YES won, 0 = NO won, NULL = not yet resolved
    resolved_at  TIMESTAMPTZ,
    alerted      BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_prop_scan_log_market
    ON prop_scan_log (market_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_prop_scan_log_game_start
    ON prop_scan_log (game_start);

-- Tanking pattern signals (NBA end-of-season motivated vs. tanking matchups)
CREATE TABLE IF NOT EXISTS tanking_signals (
    id                      SERIAL PRIMARY KEY,
    scanned_at              TIMESTAMPTZ DEFAULT NOW(),
    market_id               TEXT NOT NULL,
    game_start              TIMESTAMPTZ,
    motivated_team          TEXT,
    tanking_team            TEXT,
    motivation_differential FLOAT,
    current_price           FLOAT,
    drift_24h               FLOAT,
    pattern_strength        TEXT,    -- HIGH / MODERATE
    action                  TEXT,    -- BUY / SELL / CLOSE / WATCH
    lineup_notes            TEXT     -- JSON array of Rotowire notes, if any
);
CREATE INDEX IF NOT EXISTS idx_tanking_signals_market
    ON tanking_signals (market_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_tanking_signals_game_start
    ON tanking_signals (game_start);

-- Trading bot: open positions ledger
CREATE TABLE IF NOT EXISTS open_positions (
    id            SERIAL PRIMARY KEY,
    market_id     TEXT NOT NULL,
    slug          TEXT,
    signal_type   TEXT,           -- 'tanking' | 'prop'
    side          TEXT,           -- 'YES' | 'NO'
    token_id      TEXT,           -- ERC-1155 YES token ID from markets table
    size_usd      NUMERIC(10,2),
    size_shares   NUMERIC(12,4),
    entry_price   NUMERIC(8,4),
    entry_ts      TIMESTAMPTZ DEFAULT NOW(),
    game_start    TIMESTAMPTZ,
    status        TEXT DEFAULT 'open',  -- open | closed | cancelled
    exit_price    NUMERIC(8,4),
    exit_ts       TIMESTAMPTZ,
    pnl_usd       NUMERIC(10,2),
    clob_order_id TEXT,
    fill_status   TEXT DEFAULT 'pending',  -- pending | filled | exit_pending | cancelled | force_cancelled | timed_out | repricing | stale
    current_bid   FLOAT,                   -- latest market bid, refreshed by stop-loss monitor (used for unrealized P&L)
    exit_order_id TEXT,                    -- T-35: CLOB order id of the SELL placed by stop-loss/take-profit; order_poller finalizes
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_open_positions_market
    ON open_positions (market_id, status);

-- Cross-sport real-time drift monitor (T-30)
-- Flags markets that drift > N% over the lookback window without a coincident
-- spike event. action = 'WATCH' — drift alone is not tradeable, just a heads-up.
CREATE TABLE IF NOT EXISTS drift_signals (
    id              SERIAL PRIMARY KEY,
    scanned_at      TIMESTAMPTZ DEFAULT NOW(),
    market_id       TEXT NOT NULL,
    sport           TEXT,
    game_start      TIMESTAMPTZ,
    current_price   FLOAT,
    past_price      FLOAT,           -- price at lookback_hours ago
    lookback_hours  FLOAT,           -- e.g. 6.0
    drift_pct       FLOAT,           -- (current - past) / past * 100, signed
    direction       TEXT,            -- UP / DOWN
    has_spike       BOOLEAN DEFAULT FALSE,  -- true if a spike_event landed inside the window
    action          TEXT,            -- WATCH
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_drift_signals_market
    ON drift_signals (market_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_drift_signals_game_start
    ON drift_signals (game_start);

-- Spike follow signals (T-31) — derived from collector spike_events
CREATE TABLE IF NOT EXISTS spike_signals (
    id              SERIAL PRIMARY KEY,
    scanned_at      TIMESTAMPTZ DEFAULT NOW(),
    spike_event_id  BIGINT REFERENCES spike_events(id),
    market_id       TEXT NOT NULL,
    sport           TEXT,
    game_start      TIMESTAMPTZ,
    direction       TEXT,            -- 'up' / 'down' (matches spike_events.direction)
    magnitude       FLOAT,
    n_steps         INT,
    entry_price     FLOAT,           -- end_price of the spike (where to enter)
    action          TEXT,            -- BUY / WATCH
    notes           TEXT,
    traded          BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_spike_signals_market
    ON spike_signals (market_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_spike_signals_event
    ON spike_signals (spike_event_id);

-- NBA injury signals (key player OUT/DOUBTFUL → opponent BUY)
CREATE TABLE IF NOT EXISTS injury_signals (
    id              SERIAL PRIMARY KEY,
    scanned_at      TIMESTAMPTZ DEFAULT NOW(),
    market_id       TEXT NOT NULL,
    game_start      TIMESTAMPTZ,
    injured_team    TEXT,
    healthy_team    TEXT,
    player_name     TEXT,
    status          TEXT,           -- OUT / DOUBTFUL / QUESTIONABLE / GTD
    current_price   FLOAT,
    drift_24h       FLOAT,
    action          TEXT,           -- BUY / WATCH
    notes           TEXT,
    traded          BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_injury_signals_market
    ON injury_signals (market_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_injury_signals_game_start
    ON injury_signals (game_start);

-- MLB pitcher signals (starting pitcher mismatch opportunities)
CREATE TABLE IF NOT EXISTS pitcher_signals (
    id                   SERIAL PRIMARY KEY,
    scanned_at           TIMESTAMPTZ DEFAULT NOW(),
    market_id            TEXT NOT NULL,
    game_start           TIMESTAMPTZ,
    favored_team         TEXT,
    underdog_team        TEXT,
    home_pitcher         TEXT,
    home_era             FLOAT,
    away_pitcher         TEXT,
    away_era             FLOAT,
    era_differential     FLOAT,
    quality_differential FLOAT,
    current_price        FLOAT,
    drift_24h            FLOAT,
    signal_strength      TEXT,    -- HIGH / MODERATE / WATCH
    action               TEXT,    -- BUY / SELL / CLOSE / WATCH / NO MARKET
    traded               BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_pitcher_signals_market
    ON pitcher_signals (market_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_pitcher_signals_game_start
    ON pitcher_signals (game_start);

-- T-58: LP Rewards market-maker tables. Kept SEPARATE from open_positions
-- because directional and MM P&L lifecycles differ: MM places/cancels
-- many short-lived quotes per hour, directional tracks a single position
-- per signal. Sharing the table would make both harder to reason about.

-- Every BUY/SELL limit order placed by the MM agent. A "paired quote"
-- is two rows (one BUY, one SELL) linked by placed_at proximity on the
-- same market_id. Rows are immutable: placement creates a row, and
-- cancel/fill stamps cancelled_at + status WITHOUT deleting.
CREATE TABLE IF NOT EXISTS maker_quotes (
    id            SERIAL PRIMARY KEY,
    market_id     TEXT NOT NULL,
    slug          TEXT,
    side          TEXT NOT NULL,                -- BUY | SELL
    token_id      TEXT NOT NULL,
    price         NUMERIC(8,4) NOT NULL,
    size_shares   NUMERIC(12,4) NOT NULL,
    clob_order_id TEXT,                         -- assigned after successful CLOB POST
    placed_at     TIMESTAMPTZ DEFAULT NOW(),
    cancelled_at  TIMESTAMPTZ,
    status        TEXT DEFAULT 'LIVE'           -- LIVE | MATCHED | CANCELLED | EXPIRED | REJECTED
);
CREATE INDEX IF NOT EXISTS idx_maker_quotes_market
    ON maker_quotes (market_id, placed_at DESC);
CREATE INDEX IF NOT EXISTS idx_maker_quotes_status
    ON maker_quotes (status) WHERE status = 'LIVE';

-- Fills detected via order_poller / WebSocket. Each fill refers to the
-- parent quote row. A single quote can have multiple partial fills; we
-- accumulate fill_size until quote is MATCHED.
CREATE TABLE IF NOT EXISTS maker_fills (
    id             SERIAL PRIMARY KEY,
    quote_id       INTEGER REFERENCES maker_quotes(id),
    market_id      TEXT NOT NULL,
    side           TEXT NOT NULL,
    fill_price     NUMERIC(8,4) NOT NULL,
    fill_size      NUMERIC(12,4) NOT NULL,
    ts             TIMESTAMPTZ DEFAULT NOW(),
    clob_trade_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_maker_fills_market
    ON maker_fills (market_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_maker_fills_quote
    ON maker_fills (quote_id);

-- Weekly reward emissions. Polymarket accrues rewards per market per day;
-- we snapshot totals from /rewards/user endpoint into this table so we can
-- track earnings trajectory, reconcile expected vs actual, and mark claimed
-- status after each on-chain claim transaction.
CREATE TABLE IF NOT EXISTS rewards_history (
    id             SERIAL PRIMARY KEY,
    snapshot_ts    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    week_start     DATE NOT NULL,                -- Monday of the week this covers
    market_id      TEXT,                         -- NULL for aggregate totals
    rewards_usdc   NUMERIC(10,4) NOT NULL,
    claimed        BOOLEAN DEFAULT FALSE,
    claimed_at     TIMESTAMPTZ,
    claim_tx_hash  TEXT,
    raw_json       JSONB                         -- exact API response for audit
);
CREATE INDEX IF NOT EXISTS idx_rewards_history_week
    ON rewards_history (week_start, market_id);

-- Historical market outcomes + pre-game prices (populated by historical_fetcher.py)
-- Used by calibration_analyzer + calibration_signal --build
CREATE TABLE IF NOT EXISTS historical_calibration (
    market_id     TEXT PRIMARY KEY,
    slug          TEXT,
    sport         TEXT,
    market_type   TEXT,           -- moneyline | spreads | totals | unknown
    game_start    TIMESTAMPTZ,
    outcome       SMALLINT,       -- 1 = YES won, 0 = NO won, NULL = unresolved
    price_close   FLOAT,          -- last snapshot before game_start
    price_pre1h   FLOAT,
    price_pre2h   FLOAT,
    price_pre6h   FLOAT,
    price_pre12h  FLOAT,
    price_pre24h  FLOAT,
    price_pre48h  FLOAT,
    n_price_pts   INT DEFAULT 0,
    fetched_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_historical_calibration_sport
    ON historical_calibration (sport, market_type);

-- Calibration edges — persistent lookup table of systematic pricing biases per
-- (sport, market_type, price tier). Built offline by calibration_signal --build,
-- consumed online by the trading bot's calibration_scan.
CREATE TABLE IF NOT EXISTS calibration_edges (
    sport        TEXT NOT NULL,
    market_type  TEXT NOT NULL,        -- moneyline | spreads | totals | any
    price_lo     FLOAT NOT NULL,       -- bucket lower bound (inclusive)
    price_hi     FLOAT NOT NULL,       -- bucket upper bound (exclusive)
    avg_price    FLOAT NOT NULL,       -- average historical mid_price within this bucket
    actual_win_rate FLOAT NOT NULL,    -- fraction of YES resolutions observed
    edge_pct     FLOAT NOT NULL,       -- (actual_win_rate - avg_price) * 100
    direction    TEXT NOT NULL,        -- YES (underpriced) / NO (overpriced)
    n            INT NOT NULL,         -- sample size in the bucket
    confidence   TEXT NOT NULL,        -- HIGH (n>=50) / MEDIUM (n>=20) / LOW
    updated_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (sport, market_type, price_lo)
);
CREATE INDEX IF NOT EXISTS idx_calibration_edges_sport
    ON calibration_edges (sport, confidence, edge_pct DESC);

-- Trading bot: full order audit log
CREATE TABLE IF NOT EXISTS order_log (
    id            SERIAL PRIMARY KEY,
    market_id     TEXT NOT NULL,
    position_id   INTEGER REFERENCES open_positions(id),
    action        TEXT,           -- 'buy' | 'sell' | 'cancel'
    clob_order_id TEXT,
    price         NUMERIC(8,4),
    size_usd      NUMERIC(10,2),
    status        TEXT,           -- pending | filled | cancelled | rejected
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    filled_at     TIMESTAMPTZ,
    raw_response  TEXT
);
