CREATE TABLE price_ingestion (
 security_id VARCHAR PRIMARY KEY,
 source VARCHAR NOT NULL,
 actions_checked_from DATE NOT NULL,
 actions_checked_through DATE NOT NULL,
 price_basis_date DATE NOT NULL,
 ingested_at TIMESTAMPTZ NOT NULL
);
