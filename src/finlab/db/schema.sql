-- finlab canonical schema.
--
-- This file is the BASELINE: `finlab db init` applies it to an empty database
-- and stamps schema_version to the highest migration number present in
-- db/migrations/, because this file already contains every migration's effect.
-- Existing databases are moved forward by the numbered deltas instead.
--
-- Changing the schema means doing BOTH halves: add migrations/NNNN_name.sql
-- AND update this file. They are not allowed to drift —
-- tests/test_schema_drift.py builds one database from this file and another by
-- replaying every migration from empty, then asserts their columns and
-- constraints match exactly. That test is what makes baseline-plus-deltas safe.
--
-- Column names and types follow docs/architecture.md. Deviations are limited to
-- physical constraints and are each justified in a comment below.

-- ---------------------------------------------------------------------------
-- meta
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER   PRIMARY KEY,
  name       VARCHAR   NOT NULL,
  checksum   VARCHAR,
  applied_at TIMESTAMP NOT NULL
);

-- ---------------------------------------------------------------------------
-- L1 core
-- ---------------------------------------------------------------------------

-- One row per SEC filer. CIK is the durable key: tickers change, CIKs do not.
CREATE TABLE IF NOT EXISTS entity (
  cik             INTEGER PRIMARY KEY,
  name            VARCHAR NOT NULL,
  sic             INTEGER,
  sector          VARCHAR,          -- derived from SIC
  industry        VARCHAR,
  country_inc     VARCHAR,
  fiscal_year_end VARCHAR,          -- 'MMDD', e.g. '1231'
  first_filing    DATE,
  last_filing     DATE,
  is_active       BOOLEAN,
  updated_at      TIMESTAMP
);

-- Tradable line. Separate from entity because one filer can have several share
-- classes (GOOG/GOOGL), and because delisting is a security event, not an
-- entity event.
CREATE TABLE IF NOT EXISTS security (
  security_id      VARCHAR PRIMARY KEY,  -- synthetic, e.g. 'US:0000320193:AAPL'
  cik              INTEGER REFERENCES entity(cik),
  ticker           VARCHAR NOT NULL,
  exchange         VARCHAR,
  share_class      VARCHAR,
  currency         VARCHAR DEFAULT 'USD',
  first_date       DATE,
  last_date        DATE,                 -- NULL if still listed
  delisting_date   DATE,
  delisting_reason VARCHAR,              -- 'acquired','bankrupt','deregistered','moved'
  is_active        BOOLEAN
);

-- Tickers get recycled, so never join on ticker. Resolve ticker -> security_id
-- through this validity range instead.
CREATE TABLE IF NOT EXISTS ticker_history (
  ticker      VARCHAR,
  security_id VARCHAR REFERENCES security(security_id),
  valid_from  DATE,
  valid_to    DATE,                      -- '9999-12-31' if current
  PRIMARY KEY (ticker, valid_from)
);

-- One row per SEC submission.
-- accepted_at matters: a 10-K accepted at 16:45 ET cannot inform a trade at
-- that day's close. Signals lag to the next open.
CREATE TABLE IF NOT EXISTS filing (
  accession_number VARCHAR PRIMARY KEY,  -- '0000320193-25-000073'
  cik              INTEGER REFERENCES entity(cik),
  form             VARCHAR NOT NULL,     -- '10-K','10-Q','8-K','DEF 14A'
  filed_date       DATE NOT NULL,        -- the PIT anchor
  accepted_at      TIMESTAMP,            -- intraday
  period_of_report DATE,
  fiscal_year      INTEGER,
  fiscal_period    VARCHAR,              -- 'FY','Q1','Q2','Q3'
  is_amendment     BOOLEAN,
  amends_accession VARCHAR,
  primary_doc_url  VARCHAR,
  raw_path         VARCHAR,
  raw_sha256       VARCHAR
);

CREATE INDEX IF NOT EXISTS filing_cik_filed ON filing(cik, filed_date);

-- The point-in-time XBRL store, and the heart of the system.
--
-- Grain: one row per REPORTED OBSERVATION, not per period. The same fiscal
-- period reported in three successive filings produces three rows. That
-- redundancy is the restatement history and must be preserved.
--
-- Logical key: (cik, taxonomy, tag, unit, period_start, period_end,
-- accession_number). It is NOT declared as a physical PRIMARY KEY, for two
-- reasons:
--   1. period_start is NULL for instant (balance-sheet) facts, and DuckDB
--      forces PRIMARY KEY columns NOT NULL — every instant would be rejected.
--   2. DuckDB backs PK/UNIQUE constraints with an in-memory ART index. At the
--      ~10^8 rows this table reaches, that index alone would exceed the 9 GB
--      memory limit.
-- This table is warehouse-backed (hive-partitioned Parquet under
-- data/warehouse/fact/taxonomy=*/filed_year=*/), where no constraint is
-- enforceable anyway. `fact_key` carries that logical key as a 64-bit hash so
-- deduplication is a GROUP BY; see db/keys.py for why it is MD5-derived.
CREATE TABLE IF NOT EXISTS fact (
  fact_key         BIGINT  NOT NULL,   -- hash64 of the logical key, see db/keys.py
  cik              INTEGER NOT NULL,
  taxonomy         VARCHAR NOT NULL,   -- 'us-gaap','ifrs-full','dei','srt'
  tag              VARCHAR NOT NULL,   -- 'Revenues','Assets'
  unit             VARCHAR NOT NULL,   -- 'USD','shares','USD-per-shares'
  period_start     DATE,               -- NULL for instant facts
  period_end       DATE    NOT NULL,
  duration_days    INTEGER,            -- NULL = instant; else period_end - period_start
  duration_bucket  VARCHAR,            -- 'instant','quarter','half','ytd9','annual','other'
  value            DOUBLE  NOT NULL,
  accession_number VARCHAR NOT NULL,
  form             VARCHAR,
  fiscal_year      INTEGER,
  fiscal_period    VARCHAR,
  filed_date       DATE    NOT NULL,   -- the PIT key
  frame            VARCHAR             -- SEC's calendar-aligned frame, when present
);

-- Raw OHLCV only. Never store a vendor's adj_close: adjustment factors are
-- retroactively restated, so the adjusted series is derived from
-- corporate_action as-of a date instead.
-- Logical key (security_id, date) is not declared physically for the same
-- index-memory reason as fact; this table is warehouse-backed too, and carries
-- the same style of hashed surrogate key.
CREATE TABLE IF NOT EXISTS price_daily (
  price_key   BIGINT  NOT NULL,   -- hash64 of (security_id, date), see db/keys.py
  security_id VARCHAR NOT NULL,
  date        DATE    NOT NULL,
  open        DOUBLE,
  high        DOUBLE,
  low         DOUBLE,
  close       DOUBLE,
  volume      BIGINT,
  source      VARCHAR
);

CREATE TABLE IF NOT EXISTS corporate_action (
  security_id VARCHAR NOT NULL,
  ex_date     DATE    NOT NULL,
  action_type VARCHAR NOT NULL,   -- 'split','cash_dividend','spinoff'
  factor      DOUBLE,             -- 2.0 for a 2:1 split
  amount      DOUBLE,             -- dividend per share
  PRIMARY KEY (security_id, ex_date, action_type)
);

-- ---------------------------------------------------------------------------
-- L2 canonical
-- ---------------------------------------------------------------------------

-- The tag normalisation layer. "Revenue" appears as Revenues,
-- RevenueFromContractWithCustomerExcludingAssessedTax, SalesRevenueNet, ...
-- varying by company AND by year.
-- Resolution rule: for each (cik, canonical, period), take the lowest-priority
-- tag actually present.
CREATE TABLE IF NOT EXISTS concept_map (
  canonical  VARCHAR  NOT NULL,   -- 'revenue','net_income','total_assets','fcf'
  taxonomy   VARCHAR  NOT NULL,
  tag        VARCHAR  NOT NULL,
  priority   INTEGER  NOT NULL,   -- 1 = most preferred
  sign       SMALLINT DEFAULT 1,  -- -1 where the tag is reported with opposite sign
  valid_from DATE     DEFAULT '1900-01-01',
  valid_to   DATE     DEFAULT '9999-12-31',
  PRIMARY KEY (canonical, taxonomy, tag, valid_from)
);

-- The defence against survivorship bias. Populated daily from the Nasdaq Trader
-- symbol directory. This is the one dataset that cannot be bought retroactively
-- for free, so capture starts on day one. Until history accumulates, every
-- pre-2026 backtest is survivorship-contaminated and must say so in
-- backtest_run.caveats.
CREATE TABLE IF NOT EXISTS universe_snapshot (
  snapshot_date DATE    NOT NULL,
  security_id   VARCHAR NOT NULL,
  exchange      VARCHAR,
  is_common     BOOLEAN,
  market_cap    DOUBLE,
  PRIMARY KEY (snapshot_date, security_id)
);

-- ---------------------------------------------------------------------------
-- L3 derived
-- ---------------------------------------------------------------------------

-- inputs_hash + code_version give cheap invalidation: when a restatement lands
-- or a metric's code changes, recompute only the affected rows.
CREATE TABLE IF NOT EXISTS metric (
  cik          INTEGER NOT NULL,
  as_of_date   DATE    NOT NULL,
  metric_name  VARCHAR NOT NULL,   -- 'roic','fcf_margin','piotroski_f','ev_ebitda'
  value        DOUBLE,
  inputs_hash  VARCHAR,            -- hash of the source fact rows
  code_version VARCHAR,            -- git sha of the metric module
  computed_at  TIMESTAMP,
  PRIMARY KEY (cik, as_of_date, metric_name)
);

CREATE TABLE IF NOT EXISTS valuation_run (
  run_id       VARCHAR PRIMARY KEY,
  cik          INTEGER NOT NULL,
  as_of_date   DATE    NOT NULL,
  method       VARCHAR NOT NULL,   -- 'reverse_dcf','dcf_grid','peer_regression'
  assumptions  JSON,               -- wacc range, terminal g range, horizon
  output       JSON,               -- implied growth, fair value grid, residual
  inputs_hash  VARCHAR,
  code_version VARCHAR,
  computed_at  TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- L4 text — the only layer that spends inference quota
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS document (
  accession_number VARCHAR PRIMARY KEY REFERENCES filing(accession_number),
  doc_type         VARCHAR,
  url              VARCHAR,
  raw_path         VARCHAR,
  raw_sha256       VARCHAR,
  char_count       INTEGER,
  fetched_at       TIMESTAMP
);

CREATE TABLE IF NOT EXISTS document_section (
  section_id       VARCHAR PRIMARY KEY,  -- sha256(accession || item_code)
  accession_number VARCHAR REFERENCES document(accession_number),
  cik              INTEGER,
  item_code        VARCHAR,              -- '1','1A','7','7A','8'
  title            VARCHAR,
  text             VARCHAR,
  text_sha256      VARCHAR NOT NULL,     -- the cache key
  token_estimate   INTEGER,
  char_start       INTEGER,
  char_end         INTEGER
);

-- The cache key is text_sha256, NOT section_id: a boilerplate risk-factor
-- section repeated verbatim across four quarters hashes identically, so it is
-- extracted once and served from cache three times.
CREATE TABLE IF NOT EXISTS extraction (
  extraction_id  VARCHAR PRIMARY KEY,
  text_sha256    VARCHAR NOT NULL,
  schema_name    VARCHAR NOT NULL,   -- 'risk_factors','segment_revenue','guidance'
  schema_version INTEGER NOT NULL,
  model          VARCHAR NOT NULL,
  prompt_sha256  VARCHAR NOT NULL,
  output         JSON,
  valid          BOOLEAN,            -- passed pydantic validation
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  latency_ms     INTEGER,
  created_at     TIMESTAMP,
  UNIQUE (text_sha256, schema_name, schema_version, model, prompt_sha256)
);

-- ---------------------------------------------------------------------------
-- L5 output
-- ---------------------------------------------------------------------------

-- The composer receives fact_bundle and may not emit a number absent from it.
-- The critic re-extracts every numeric claim and checks it. A report with any
-- 'unsupported' or 'mismatch' claim never reaches 'verified'.
CREATE TABLE IF NOT EXISTS report (
  report_id        VARCHAR PRIMARY KEY,
  cik              INTEGER NOT NULL,
  as_of_date       DATE    NOT NULL,
  template_version VARCHAR,
  fact_bundle      JSON,      -- every number the composer was allowed to use
  markdown         VARCHAR,
  status           VARCHAR,   -- 'draft','verified','rejected'
  created_at       TIMESTAMP
);

CREATE TABLE IF NOT EXISTS claim (
  claim_id      VARCHAR PRIMARY KEY,
  report_id     VARCHAR REFERENCES report(report_id),
  claim_text    VARCHAR,
  numeric_value DOUBLE,
  unit          VARCHAR,
  cited_key     VARCHAR,   -- path into fact_bundle, e.g. 'metrics.roic.2025Q4'
  verdict       VARCHAR,   -- 'match','mismatch','unsupported'
  delta         DOUBLE
);

-- ---------------------------------------------------------------------------
-- research
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS signal (
  signal_id       VARCHAR PRIMARY KEY,
  name            VARCHAR,
  definition_sql  VARCHAR,
  definition_hash VARCHAR,
  code_version    VARCHAR
);

CREATE TABLE IF NOT EXISTS signal_value (
  signal_id        VARCHAR NOT NULL,
  security_id      VARCHAR NOT NULL,
  as_of_date       DATE    NOT NULL,
  raw_value        DOUBLE,
  zscore           DOUBLE,
  sector_neutral_z DOUBLE,
  PRIMARY KEY (signal_id, security_id, as_of_date)
);

-- The multiple-testing ledger. Rows are inserted BEFORE each backtest, not
-- after, so that n_prior_trials is structurally awkward to forget.
CREATE TABLE IF NOT EXISTS research_trial (
  trial_id    VARCHAR PRIMARY KEY,
  signal_id   VARCHAR,
  config_hash VARCHAR,
  created_at  TIMESTAMP,
  note        VARCHAR
);

CREATE TABLE IF NOT EXISTS backtest_run (
  run_id         VARCHAR PRIMARY KEY,
  signal_id      VARCHAR,
  universe_def   VARCHAR,
  start_date     DATE,
  end_date       DATE,
  rebalance      VARCHAR,   -- 'monthly','quarterly'
  cost_bps       DOUBLE,
  slippage_model VARCHAR,
  config         JSON,
  git_sha        VARCHAR,
  n_prior_trials INTEGER,   -- feeds the deflated Sharpe
  caveats        VARCHAR,   -- e.g. 'survivorship-contaminated pre-2026'
  created_at     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS backtest_metric (
  run_id      VARCHAR REFERENCES backtest_run(run_id),
  metric_name VARCHAR,   -- 'sharpe','deflated_sharpe','max_dd','turnover'
  value       DOUBLE,
  PRIMARY KEY (run_id, metric_name)
);

-- ---------------------------------------------------------------------------
-- orchestration
-- ---------------------------------------------------------------------------

-- State machine: pending -> running -> done | failed | deadletter.
-- Retry with backoff via not_before; three strikes to deadletter.
CREATE TABLE IF NOT EXISTS job (
  job_id     VARCHAR PRIMARY KEY,
  job_type   VARCHAR NOT NULL,     -- 'fetch_filing','extract','compose','backtest'
  payload    JSON,
  priority   INTEGER DEFAULT 100,  -- lower runs first
  state      VARCHAR DEFAULT 'pending',
  attempts   INTEGER DEFAULT 0,
  not_before TIMESTAMP,            -- exponential backoff
  locked_by  VARCHAR,
  locked_at  TIMESTAMP,
  error      VARCHAR,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS job_claim ON job(state, priority, not_before);
