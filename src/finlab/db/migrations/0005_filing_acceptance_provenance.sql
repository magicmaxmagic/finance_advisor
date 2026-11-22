ALTER TABLE filing ADD COLUMN accepted_at_raw VARCHAR;
ALTER TABLE filing ADD COLUMN accepted_at_status VARCHAR DEFAULT 'unverified';
ALTER TABLE filing ADD COLUMN accepted_at_utc TIMESTAMPTZ;
