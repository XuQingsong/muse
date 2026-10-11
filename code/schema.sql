-- quant 库表结构。约定：金额单位一律美元；日期 date；抓取时点 timestamptz。
-- 预期快照按 snapshot_date 追加，不覆盖历史 —— 这是 PIT 验证的根基。
--
-- 2026-10-08 起按三层 schema 分放（本文件是未分层时的旧版 DDL，仅作字段参考；
-- 实际结构以下方映射与 pg_dump 为准，新项目请先 SET search_path）：
--   data:     estimates_snapshot, estimates_trend, estimates_revisions,
--             price_targets, earnings_history, grade_actions, prices_daily,
--             vix_daily, etf_options_daily, options_snapshot_daily,
--             stock_iv_panel, volume_factors_daily, insider_transactions,
--             insider_summary, eightk_filings, short_interest, financial_facts
--   strategy: paper_trades, paper_equity
--   research: pit_book_scores, raw_json
-- 连接后执行：SET search_path TO data, strategy, research, public;

CREATE TABLE IF NOT EXISTS estimates_snapshot (
    snapshot_date date NOT NULL,
    ticker        text NOT NULL,
    source        text NOT NULL DEFAULT 'yfinance',
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_date, ticker)
);

CREATE TABLE IF NOT EXISTS estimates_trend (
    snapshot_date date NOT NULL,
    ticker        text NOT NULL,
    period        text NOT NULL,          -- 0q / +1q / 0y / +1y（Yahoo 口径）
    cur           numeric,
    ago7          numeric,
    ago30         numeric,
    ago60         numeric,
    ago90         numeric,
    rev90_pct     numeric,
    PRIMARY KEY (snapshot_date, ticker, period)
);

CREATE TABLE IF NOT EXISTS estimates_revisions (
    snapshot_date date NOT NULL,
    ticker        text NOT NULL,
    period        text NOT NULL,          -- 0q / 0y
    up30 int, down30 int, up7 int, down7 int,
    PRIMARY KEY (snapshot_date, ticker, period)
);

CREATE TABLE IF NOT EXISTS price_targets (
    snapshot_date date NOT NULL,
    ticker        text NOT NULL,
    price   numeric,                       -- 抓取时股价
    mean    numeric,
    median  numeric,
    high    numeric,
    low     numeric,
    PRIMARY KEY (snapshot_date, ticker)
);

CREATE TABLE IF NOT EXISTS earnings_history (
    ticker       text NOT NULL,
    quarter_date date NOT NULL,
    eps_actual   numeric,
    eps_est      numeric,
    surprise_pct numeric,
    first_seen   date NOT NULL DEFAULT CURRENT_DATE,
    PRIMARY KEY (ticker, quarter_date)
);

CREATE TABLE IF NOT EXISTS grade_actions (
    ticker      text NOT NULL,
    action_date date NOT NULL,
    firm        text NOT NULL,
    from_grade  text,
    to_grade    text,
    action      text NOT NULL,             -- up / down / main / reit / init
    source      text NOT NULL DEFAULT 'yahoo',
    PRIMARY KEY (ticker, action_date, firm, action, to_grade)
);

CREATE TABLE IF NOT EXISTS prices_daily (
    ticker text NOT NULL,
    date   date NOT NULL,
    open numeric, high numeric, low numeric, close numeric,
    adj_close numeric,
    volume bigint,
    source text NOT NULL DEFAULT 'github-etf-dataset',
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS vix_daily (
    date date PRIMARY KEY,
    vix  numeric
);

CREATE TABLE IF NOT EXISTS etf_options_daily (
    symbol text NOT NULL,                  -- SPY / QQQ
    date   date NOT NULL,
    pc_vol numeric, pc_oi numeric, total_vol bigint,
    iv_atm numeric, skew25 numeric,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS options_snapshot_daily (
    ticker text NOT NULL,
    date   date NOT NULL,
    price numeric, iv30 numeric, iv30_chg_pct numeric,
    pc_vol numeric, pc_oi numeric,
    call_vol numeric, put_vol numeric, skew25 numeric,
    asof timestamptz,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS stock_iv_panel (
    symbol text NOT NULL,
    date   date NOT NULL,
    strikes_spread numeric,
    calls_traded bigint, puts_traded bigint,
    calls_oi bigint, puts_oi bigint,
    expirations int,
    iv_ditm numeric, iv_itm numeric, iv_sitm numeric, iv_atm numeric,
    iv_sotm numeric, iv_otm numeric, iv_dotm numeric,
    contracts_number int,
    hv20 numeric, hv40 numeric, hv60 numeric, hv75 numeric,
    hv90 numeric, hv120 numeric, hv180 numeric, hv200 numeric,
    vix numeric,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS volume_factors_daily (
    date date NOT NULL,
    ticker text NOT NULL,
    vol_ratio numeric, up_share numeric, accdist numeric,
    dist_days int, obv_trend numeric,
    price_obv_div boolean, breakout boolean, climax boolean,
    vol_score numeric,
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS insider_transactions (
    ticker text NOT NULL,
    filed  date NOT NULL,
    txn_date date,
    code text,                             -- P 买 / S 卖 / A 授予 / M 行权
    shares numeric, price numeric,
    owner text,
    is_officer boolean, is_director boolean,
    PRIMARY KEY (ticker, filed, txn_date, owner, code, shares)
);

CREATE TABLE IF NOT EXISTS insider_summary (
    ticker text PRIMARY KEY,
    payload jsonb NOT NULL,                -- info_insider_2026 聚合原样
    loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eightk_filings (
    ticker text NOT NULL,
    filed  date NOT NULL,
    accepted timestamptz,
    items text[], key_items text[],
    summary jsonb,
    doc text,
    ex99_found boolean, ex_file text,
    guidance jsonb, hits jsonb,
    buyback boolean, dividend boolean,
    text_len int,
    PRIMARY KEY (ticker, filed, doc)
);

CREATE TABLE IF NOT EXISTS short_interest (
    ticker text NOT NULL,
    settlement_date date NOT NULL,
    short_shares bigint,
    avg_daily_vol bigint,
    days_to_cover numeric,
    PRIMARY KEY (ticker, settlement_date)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    version text NOT NULL,
    trade_date date, ticker text, action text,
    shares numeric, price numeric, amount numeric,
    trigger text, layer text,
    PRIMARY KEY (version, trade_date, ticker, action, shares, price)
);

CREATE TABLE IF NOT EXISTS paper_equity (
    version text NOT NULL,
    date date NOT NULL,
    equity numeric,
    PRIMARY KEY (version, date)
);

CREATE TABLE IF NOT EXISTS raw_json (
    name text NOT NULL,
    ticker text,
    payload jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name, ticker)
);
