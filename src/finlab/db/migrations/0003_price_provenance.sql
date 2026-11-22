ALTER TABLE price_daily ADD COLUMN adjusted_close DOUBLE;
ALTER TABLE price_daily ADD COLUMN adjustment_basis VARCHAR;
ALTER TABLE price_daily ADD COLUMN ingested_at TIMESTAMPTZ;
ALTER TABLE corporate_action ADD COLUMN source VARCHAR;
ALTER TABLE corporate_action ADD COLUMN ingested_at TIMESTAMPTZ;
