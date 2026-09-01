-- MLS v4.1 Flow × Chips preview schema
-- All tables are plugin-owned. No ALTER/UPDATE/DELETE against existing MLS schema.

CREATE TABLE IF NOT EXISTS intraday_snapshot (
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    ts TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    prev_close REAL,
    volume REAL,
    ma5_volume REAL,
    vwap REAL,
    a_flow REAL,
    net_active REAL,
    bid_ask_ratio REAL,
    net_flow_amount REAL,
    turnover_ratio REAL,
    price_change_pct REAL,
    price_data_date TEXT,
    flow_data_time TEXT,
    as_of TEXT NOT NULL,
    PRIMARY KEY (trade_date, symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_intraday_latest ON intraday_snapshot(trade_date, symbol, ts);

CREATE TABLE IF NOT EXISTS chip_daily (
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    foreign_net_lots REAL,
    institutional_net_lots REAL,
    volume_lots REAL,
    big_holder_trend REAL,
    chip_data_date TEXT,
    as_of TEXT NOT NULL,
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS trigger_context (
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trigger_price REAL,
    monitor_price REAL,
    trigger_failed INTEGER,
    trigger_passed INTEGER,
    source_data_date TEXT,
    as_of TEXT NOT NULL,
    source_note TEXT,
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS market_regime_daily (
    trade_date TEXT PRIMARY KEY,
    regime TEXT,
    index_return REAL,
    baseline_up_rate REAL,
    as_of TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flow_threshold_config (
    threshold_key TEXT PRIMARY KEY,
    min_amount_threshold REAL NOT NULL,
    window_ticks_required INTEGER NOT NULL DEFAULT 2,
    note TEXT
);

CREATE TABLE IF NOT EXISTS decision_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT,
    symbol TEXT,
    scenario TEXT NOT NULL,
    state TEXT,
    market_regime TEXT,
    success INTEGER,
    next_day_up INTEGER,
    plus3 INTEGER,
    plus5 INTEGER,
    mfe REAL,
    mae REAL,
    baseline_up_rate REAL,
    was_false_kill INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_decision_history_scenario ON decision_history(scenario, market_regime);

CREATE TABLE IF NOT EXISTS false_kill_kpi (
    trade_date TEXT PRIMARY KEY,
    failed_total INTEGER,
    false_kill_count INTEGER,
    false_kill_rate REAL,
    freshness_pass_rate REAL,
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
