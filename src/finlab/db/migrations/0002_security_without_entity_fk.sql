-- Drop the foreign key from security.cik to entity.cik.
--
-- The universe snapshot records every listed security on the exchange — around
-- 13,000 of them — while `entity` only holds filers whose facts we have
-- ingested. Until M1 loads the full companyfacts archive that is 54 companies,
-- so nearly every security legitimately references a CIK that is not in
-- `entity` yet. The constraint made capturing the universe impossible, and the
-- universe is the one dataset that cannot be backfilled later.
--
-- The relationship still holds logically: security.cik is a CIK. It is simply
-- not enforceable while the two tables cover different populations.
--
-- DuckDB cannot drop a constraint in place, so both tables are rebuilt.
-- ticker_history goes first because it references security.

CREATE TEMP TABLE _security_backup AS SELECT * FROM security;
CREATE TEMP TABLE _ticker_history_backup AS SELECT * FROM ticker_history;

DROP TABLE ticker_history;
DROP TABLE security;

CREATE TABLE security (
  security_id      VARCHAR PRIMARY KEY,  -- synthetic, e.g. 'US:0000320193:AAPL'
  cik              INTEGER,              -- a CIK, but not FK-enforceable: see above
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

CREATE TABLE ticker_history (
  ticker      VARCHAR,
  security_id VARCHAR REFERENCES security(security_id),
  valid_from  DATE,
  valid_to    DATE,                      -- '9999-12-31' if current
  PRIMARY KEY (ticker, valid_from)
);

INSERT INTO security SELECT * FROM _security_backup;
INSERT INTO ticker_history SELECT * FROM _ticker_history_backup;

DROP TABLE _security_backup;
DROP TABLE _ticker_history_backup;
