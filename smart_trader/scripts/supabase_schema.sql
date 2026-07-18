-- Supabase table definitions for smart-trader.
-- Run this in the Supabase SQL Editor (Dashboard → SQL Editor → New query).
--
-- Tables:
--   1. smart_money_filings      ← smart_money.py (5 original providers)
--   2. smart_money_candidates   ← smart_money.py (conviction-scored output)
--   3. ohlcv_bars               ← ohlcv_store.py (OHLCV price cache)
--   4. portfolio_snapshots      ← portfolio_store.py (snapshot metadata)
--   5. portfolio_stocks         ← portfolio_store.py (scored universe)
--   6. portfolio_stock_funds    ← portfolio_store.py (per-fund holdings)
--   7. fund_holdings_raw        ← portfolio_store.py (raw holdings archive)
--   8. sectors                  ← sector_resolver.py (yfinance sector cache)
--   9. exit_state_kv            ← portfolio_store.py (signal-driven exit state)

-- ============================================================
-- 1. smart_money_filings
-- ============================================================
CREATE TABLE IF NOT EXISTS smart_money_filings (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source         TEXT NOT NULL,
    actor          TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    tx_type        TEXT NOT NULL,
    dollar_amount  DOUBLE PRECISION,
    share_change   INTEGER,
    filing_date    DATE NOT NULL,
    trade_date     DATE NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_smf_source ON smart_money_filings(source);
CREATE INDEX IF NOT EXISTS idx_smf_symbol ON smart_money_filings(symbol);
CREATE INDEX IF NOT EXISTS idx_smf_filing_date ON smart_money_filings(filing_date);

-- ============================================================
-- 2. smart_money_candidates
-- ============================================================
CREATE TABLE IF NOT EXISTS smart_money_candidates (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol           TEXT NOT NULL,
    conviction_score DOUBLE PRECISION NOT NULL,
    sources          JSONB,
    generated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    vol_rank         DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_smc_symbol ON smart_money_candidates(symbol);
CREATE INDEX IF NOT EXISTS idx_smc_generated ON smart_money_candidates(generated_at);

-- ============================================================
-- 3. ohlcv_bars
-- ============================================================
CREATE TABLE IF NOT EXISTS ohlcv_bars (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol      TEXT NOT NULL,
    date        DATE NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      BIGINT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol ON ohlcv_bars(symbol);
CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON ohlcv_bars(date);

-- ============================================================
-- 4. portfolio_snapshots
-- ============================================================
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    generated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    top_n_size     INTEGER NOT NULL,
    universe_size  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_generated ON portfolio_snapshots(generated_at);

-- ============================================================
-- 5. portfolio_stocks (now references portfolio_snapshots)
-- ============================================================
DROP TABLE IF EXISTS portfolio_stocks;
CREATE TABLE portfolio_stocks (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id         BIGINT NOT NULL REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE,
    generated_at        TIMESTAMPTZ NOT NULL,
    symbol              TEXT NOT NULL,
    rank                INTEGER NOT NULL,
    in_top_n            BOOLEAN NOT NULL DEFAULT FALSE,
    composite_score     DOUBLE PRECISION NOT NULL,
    overlap_count       INTEGER NOT NULL DEFAULT 0,
    avg_holding_weight  DOUBLE PRECISION NOT NULL DEFAULT 0,
    performance_score   DOUBLE PRECISION NOT NULL DEFAULT 0,
    momentum_score      DOUBLE PRECISION NOT NULL DEFAULT 0,
    relative_strength   DOUBLE PRECISION NOT NULL DEFAULT 0,
    optimal_entry_price DOUBLE PRECISION,
    UNIQUE (snapshot_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_ps_snapshot ON portfolio_stocks(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_ps_symbol ON portfolio_stocks(symbol);
CREATE INDEX IF NOT EXISTS idx_ps_generated ON portfolio_stocks(generated_at);

-- ============================================================
-- 6. portfolio_stock_funds
-- ============================================================
CREATE TABLE IF NOT EXISTS portfolio_stock_funds (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id    BIGINT NOT NULL REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE,
    symbol         TEXT NOT NULL,
    fund_name      TEXT NOT NULL,
    provider_name  TEXT NOT NULL,
    holding_weight DOUBLE PRECISION NOT NULL,
    share_count    INTEGER NOT NULL,
    market_value   DOUBLE PRECISION NOT NULL,
    as_of_date     DATE NOT NULL,
    UNIQUE (snapshot_id, symbol, fund_name)
);
CREATE INDEX IF NOT EXISTS idx_psf_snapshot ON portfolio_stock_funds(snapshot_id);

-- ============================================================
-- 7. fund_holdings_raw
-- ============================================================
CREATE TABLE IF NOT EXISTS fund_holdings_raw (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fund_name      TEXT NOT NULL,
    provider_name  TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    share_count    INTEGER NOT NULL,
    holding_weight DOUBLE PRECISION NOT NULL,
    market_value   DOUBLE PRECISION NOT NULL,
    as_of_date     DATE NOT NULL,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (fund_name, symbol, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_fhr_fund_date ON fund_holdings_raw(fund_name, as_of_date);
CREATE INDEX IF NOT EXISTS idx_fhr_symbol ON fund_holdings_raw(symbol);

-- ============================================================
-- 8. sectors (sector cache for stocks)
-- ============================================================
CREATE TABLE IF NOT EXISTS sectors (
    symbol      TEXT PRIMARY KEY,
    sector      TEXT NOT NULL DEFAULT 'unknown',
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sectors_sector ON sectors(sector);

-- ============================================================
-- 9. exit_state_kv
-- ============================================================
CREATE TABLE IF NOT EXISTS exit_state_kv (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Row-Level Security
-- ============================================================
ALTER TABLE smart_money_filings ENABLE ROW LEVEL SECURITY;
ALTER TABLE smart_money_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE ohlcv_bars ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_stocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_stock_funds ENABLE ROW LEVEL SECURITY;
ALTER TABLE fund_holdings_raw ENABLE ROW LEVEL SECURITY;
ALTER TABLE exit_state_kv ENABLE ROW LEVEL SECURITY;
ALTER TABLE sectors ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all" ON smart_money_filings FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON smart_money_candidates FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON ohlcv_bars FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON portfolio_snapshots FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON portfolio_stocks FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON portfolio_stock_funds FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON fund_holdings_raw FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON exit_state_kv FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON sectors FOR ALL USING (true) WITH CHECK (true);
