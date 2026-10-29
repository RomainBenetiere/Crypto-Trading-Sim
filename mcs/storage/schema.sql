-- Schéma unique de la base (docs/SPEC.md §10).
-- Conventions : montants, prix et quantités en TEXT (Decimal sans perte) ;
-- horodatages en millisecondes UTC (INTEGER) ; booléens en INTEGER 0/1.

CREATE TABLE IF NOT EXISTS simulations (
    id                INTEGER PRIMARY KEY,
    pair_id           INTEGER NOT NULL,
    mode              TEXT    NOT NULL CHECK (mode IN ('typical', 'optimized')),
    family            TEXT    NOT NULL,
    config_json       TEXT    NOT NULL,
    draw_json         TEXT    NOT NULL DEFAULT '{}',
    seed              INTEGER NOT NULL,
    train_score       TEXT,
    validation_score  TEXT,
    created_at        INTEGER NOT NULL,
    optimized_at      INTEGER,
    UNIQUE (pair_id, mode)
);

CREATE TABLE IF NOT EXISTS indicator_params (
    id                   INTEGER PRIMARY KEY,
    sim_id               INTEGER NOT NULL REFERENCES simulations (id) ON DELETE CASCADE,
    role                 TEXT    NOT NULL CHECK (role IN ('main', 'confirmation', 'trend_filter')),
    indicator            TEXT    NOT NULL,
    params_json          TEXT    NOT NULL,
    typical_params_json  TEXT    NOT NULL,
    weight               TEXT,
    score_threshold      TEXT,
    UNIQUE (sim_id, role, indicator)
);

CREATE TABLE IF NOT EXISTS markets (
    market               TEXT    PRIMARY KEY,
    base                 TEXT    NOT NULL,
    quote                TEXT    NOT NULL,
    status               TEXT    NOT NULL,
    in_basket            INTEGER NOT NULL DEFAULT 1,
    selected             INTEGER NOT NULL DEFAULT 0,
    history_days         INTEGER,
    first_daily_ts       INTEGER,
    spread_estimate_pct  TEXT,
    volume_24h_eur       TEXT,
    atr_4h_pct           TEXT,
    tick_size            TEXT,
    quantity_decimals    INTEGER,
    notional_decimals    INTEGER,
    min_order_base       TEXT,
    min_order_quote      TEXT,
    max_order_base       TEXT,
    max_order_quote      TEXT,
    fee_category         TEXT,
    updated_at           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS candles (
    market    TEXT    NOT NULL,
    interval  TEXT    NOT NULL CHECK (interval IN ('1m', '1h', '4h', '1d')),
    ts        INTEGER NOT NULL,
    open      TEXT    NOT NULL,
    high      TEXT    NOT NULL,
    low       TEXT    NOT NULL,
    close     TEXT    NOT NULL,
    volume    TEXT    NOT NULL,
    PRIMARY KEY (market, interval, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY,
    sim_id        INTEGER NOT NULL REFERENCES simulations (id) ON DELETE CASCADE,
    market        TEXT    NOT NULL,
    order_type    TEXT    NOT NULL CHECK (order_type IN ('market', 'limit')),
    side          TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
    status        TEXT    NOT NULL CHECK (status IN ('open', 'filled', 'cancelled', 'expired')),
    price         TEXT,
    quantity      TEXT    NOT NULL,
    notional      TEXT,
    fee           TEXT,
    liquidity     TEXT    CHECK (liquidity IN ('maker', 'taker')),
    reserved      TEXT,
    reason        TEXT    NOT NULL CHECK (reason IN (
                      'signal', 'stop', 'trailing', 'take_profit', 'reverse_signal',
                      'max_duration', 'trend_filter', 'circuit_breaker')),
    book_bid      TEXT,
    book_ask      TEXT,
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER,
    filled_at     INTEGER
);
CREATE INDEX IF NOT EXISTS orders_sim ON orders (sim_id, created_at);
CREATE INDEX IF NOT EXISTS orders_open ON orders (status, market);

CREATE TABLE IF NOT EXISTS positions (
    id               INTEGER PRIMARY KEY,
    sim_id           INTEGER NOT NULL REFERENCES simulations (id) ON DELETE CASCADE,
    market           TEXT    NOT NULL,
    lots             INTEGER NOT NULL,
    quantity         TEXT    NOT NULL,
    avg_entry_price  TEXT    NOT NULL,
    invested_cost    TEXT    NOT NULL,
    buy_fees         TEXT    NOT NULL,
    stop             TEXT,
    trailing         TEXT,
    take_profit      TEXT,
    state_json       TEXT    NOT NULL DEFAULT '{}',
    opened_at        INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE (sim_id, market)
);

CREATE TABLE IF NOT EXISTS portfolio_values (
    sim_id       INTEGER NOT NULL REFERENCES simulations (id) ON DELETE CASCADE,
    ts           INTEGER NOT NULL,
    total_value  TEXT    NOT NULL,
    cash         TEXT    NOT NULL,
    PRIMARY KEY (sim_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS latest_prices (
    market    TEXT    PRIMARY KEY,
    price     TEXT    NOT NULL,
    best_bid  TEXT,
    best_ask  TEXT,
    ts        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    sim_id            INTEGER PRIMARY KEY REFERENCES simulations (id) ON DELETE CASCADE,
    pnl_eur           TEXT    NOT NULL,
    pnl_pct           TEXT    NOT NULL,
    max_drawdown_pct  TEXT    NOT NULL,
    closed_trades     INTEGER NOT NULL,
    win_rate          TEXT,
    total_fees        TEXT    NOT NULL,
    bh_gap_eur        TEXT,
    peak_value        TEXT,
    open_positions    INTEGER NOT NULL DEFAULT 0,
    updated_at        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS buy_and_hold (
    market         TEXT    PRIMARY KEY,
    weight         TEXT    NOT NULL,
    allocated_eur  TEXT    NOT NULL,
    buy_fee        TEXT    NOT NULL,
    start_price    TEXT    NOT NULL,
    quantity       TEXT    NOT NULL,
    bought_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS engine_state (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    status               TEXT    NOT NULL DEFAULT 'NOT_STARTED'
                                 CHECK (status IN ('NOT_STARTED', 'RUNNING', 'HOLD')),
    optimization_status  TEXT    NOT NULL DEFAULT 'TODO'
                                 CHECK (optimization_status IN ('TODO', 'RUNNING', 'DONE')),
    optimized_at         INTEGER,
    master_seed          INTEGER,
    basket_json          TEXT,
    heartbeat_at         INTEGER,
    started_at           INTEGER,
    active_ms            INTEGER NOT NULL DEFAULT 0,
    hold_ms              INTEGER NOT NULL DEFAULT 0,
    updated_at           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run_periods (
    id          INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL CHECK (kind IN ('active', 'hold', 'outage')),
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER
);

CREATE TABLE IF NOT EXISTS commands (
    id            INTEGER PRIMARY KEY,
    command       TEXT    NOT NULL CHECK (command IN ('optimize', 'start', 'stop', 'resume', 'reset')),
    requested_at  INTEGER NOT NULL,
    processed_at  INTEGER,
    result        TEXT
);
CREATE INDEX IF NOT EXISTS commands_pending ON commands (processed_at, id);

CREATE TABLE IF NOT EXISTS sim_state (
    sim_id      INTEGER PRIMARY KEY REFERENCES simulations (id) ON DELETE CASCADE,
    state_json  TEXT    NOT NULL,
    updated_at  INTEGER NOT NULL
);

-- Mode production (SPEC §15) : scénario choisi, figé au moment du choix (copie de la
-- configuration, indépendante d'une remise à zéro ou d'un nouveau plan) ; historique des choix.
CREATE TABLE IF NOT EXISTS production_choices (
    id                INTEGER PRIMARY KEY,
    sim_id            INTEGER NOT NULL,
    mode              TEXT    NOT NULL,
    family            TEXT    NOT NULL,
    config_json       TEXT    NOT NULL,
    draw_json         TEXT    NOT NULL,
    metric            TEXT    NOT NULL CHECK (metric IN ('pnl', 'calmar')),
    pnl_pct           TEXT,
    max_drawdown_pct  TEXT,
    closed_trades     INTEGER,
    capital           TEXT    NOT NULL,
    scale             TEXT    NOT NULL,
    chosen_at         INTEGER NOT NULL,
    replaced_at       INTEGER
);
