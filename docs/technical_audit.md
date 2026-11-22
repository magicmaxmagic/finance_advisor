# Technical audit — 2026-11-21

Audited local commit `aadf7f3` (clean initial working tree). Remote freshness was not verified. Findings are based on source inspection, not README claims.

## Architecture and data flow

Python entry points already separate engine, daemon and Analytics. Providers feed raw caches and working Parquet datasets; DuckDB stores identity, mappings, jobs and derived metadata. Advisory flock enforces one writer. FastAPI reads Parquet into in-memory DuckDB; React consumes HTTP. Publication originally hashed working directories and replaced only JSON manifests: readers were neither pinned to an epoch nor required to validate it. This is the first remediation priority.

## Implemented features

SEC companyfacts ingestion and submissions metadata; date-based PIT macros with restatement vintages and Q4 derivation; canonical concepts; immutable daily universe snapshots; Yahoo, Stooq and Alpaca integrations; price-key deduplication; queue retries and stale-job reclamation; deterministic metrics; weekly archive generation; independent CLIs; four React views (overview, ranking, company, data).

## Missing and partial features

`research/backtest.py`, `signals.py` and `stats.py` are stubs. No operational walk-forward, transaction portfolio ledger, USD/CAD accounting, scenario engine or recommendation outcome evaluation. Factor/scoring modules exist but the API implements a separate simplistic sign-count score. Sector normalization and historical model tracking are incomplete. Weekly reports default to direct Alpaca downloads or CSV rather than warehouse-only research. Optional text modules exist; cloud inference is mentioned in old architecture but is not required for deterministic analytics.

## Schema

Baseline plus migrations 0001/0002 define entity, security, ticker_history, filing, fact, price_daily, corporate_action, concept_map, universe_snapshot, metric, valuation_run, document/sections/extraction, report/claim, signal/value, research_trial, backtest_run/metric and job. Some tables support planned features rather than operational implementations. Large fact/price datasets use Parquet views; small native tables are exported separately. Accepted timestamps are stored but the principal PIT contract is date based, insufficient alone for intraday decisions.

## Provider capabilities and correctness risks

SEC is authoritative for filed US fundamentals and filing availability. Yahoo supplies daily OHLCV and actions, but adjusted close is discarded by the loader, adjustment conventions are insufficiently explicit and actions lack full published provenance. Stooq and Alpaca must not be treated as interchangeable adjustment feeds. Current membership cannot reconstruct historical delistings. Historical simulations before captured membership need explicit survivorship caveats.

Price ingestion merges incoming observations by stable security/date, but reads and rewrites the whole price history. Daemon Yahoo refresh uses a fixed ten-day window, so longer outages and older corrections may be missed. Scheduler prices run at 06:00 rather than after US close, with no exchange holiday gate. No launchd configuration exists. Provider failure, partial symbol downloads and action correction handling need stronger completeness checks.

## Financial and architectural weaknesses

Market factor lookbacks shrink to available history while retaining long-horizon names; 12-minus-1 formula is wrong; benchmark input is unused; MACD field actually reports histogram. Scoring allows missing factors to produce numeric overall scores. API uses raw facts instead of canonical PIT macros, RMS returns rather than sample volatility, and requests only 252 prices for a 252-interval return, making 12-month momentum unavailable. Company endpoint reads raw facts and assumes all optional tables exist. Published checksums were not enforced. Directory replacement has a gap and is not a transaction across datasets.

## Performance and tests

Baseline: 198 passed in 7.95s. Ruff lint passed; 89 files formatted. React TypeScript/Vite build and oxlint passed. Core tests use fixtures. Existing boundary test checks only Analytics source imports, excluding transitive API/research paths. Snapshot test uses placeholder bytes, not valid Parquet. No complete backtest, portfolio or live-provider integration evidence. Per-security API SQL and repeated scoring scans scale poorly; full-history merge and full snapshot hashing add I/O.

Initial uv invocations were blocked by sandbox cache permissions; baseline was rerun with approved cache access. No production data was downloaded or altered.

## Prioritized plan

1. Publish immutable epoch contents, pin Analytics readers, fail closed on invalid manifests, test concurrent publication and tampering. Preserve working readers for engine derivations.
2. Fix adjustment/provenance contract, incremental per-security watermarks, outage catch-up, action corrections and explicit quality gates; add after-close exchange scheduling and launchd.
3. Correct and document factors, canonical PIT API queries, missing-data statuses and explainable normalized rankings.
4. Implement lagged-execution backtests with total returns, historical membership, trial ledger, costs and walk-forward fixtures before exposing strategy results.
5. Implement separate portfolio storage and FX-aware accounting; then frontend research and weekly warehouse-only archives.
6. Optional local explanations only after deterministic milestones are validated.

## Scope and remaining work

This audit is an engineering inventory, not evidence that all requested milestones are complete. See the architecture publication addendum and session delivery notes for verified changes. No financial performance claims have been validated.
