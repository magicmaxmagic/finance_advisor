# Milestone 0 Implementation Audit

**Audit date:** 2026-11-21
**Scope:** repository structure, entry points, backend, ingestion, DuckDB/Parquet, PIT, metrics, weekly/live recommendation logic, daemon, React/Streamlit frontends, tests, deployment, and roadmap readiness.

This is an audit only. No source, schema, configuration, or test behavior was changed.

## Executive summary

Finlab is a viable PoC vertical slice, not yet the architecture described by M1-M7. The strongest parts are the fact-key/deduplication pipeline, PIT macros and regression tests, warehouse publication, writer locking, the universe snapshot, queue primitives, and the live/weekly rules engine. The repository is explicitly further behind in text extraction, reports/critic, valuation, signals, and backtesting.

The baseline is not green: `uv run pytest` has 185 passing, 4 skipped, and 2 failing tests. The frontend production build and `oxlint` both pass. The failures are in Streamlit UI assumptions and should be investigated before further implementation milestones.

## Baseline results

### Python

Command: `uv run pytest`

- 185 passed
- 4 skipped
- 2 failed
- 2 warnings
- Result: **not green**

Failures:

1. `tests/test_weekly.py::test_weekly_ui_from_blank_checkout`
   - Expected the sidebar to default to `"Weekly brief"`.
   - Actual default is `"Marché en direct"` because `PAGES` and page selection currently put live market first.
2. `tests/test_weekly.py::test_archived_ui_selects_real_edition`
   - Test assumes `app.selectbox[0].value` is a `Path` and accesses `.stem`.
   - Actual value is a string week date.

The remaining suite, including mandatory PIT, writer-lock, schema-drift, and read-path tests, passed.

Stated repository claim versus observed result:

- `README.md` says 120 tests, zero skipped.
- The observed suite is larger and has 4 skips plus 2 failures.

### Frontend

Command: `cd frontend && npm run build && npm run lint`

- `npm run build`: passed
  - TypeScript/Vite build completed
  - Vite produced `dist/index.html`, CSS, and JavaScript assets
- `npm run lint`: passed (`oxlint`, no reported issues)

No frontend test script or test runner is configured. The package has no Vitest/Jest/Playwright dependency and no frontend test files were found.

## Fully implemented

### Entry points and process model

- Typer CLI covers database init/migrate/status, PoC ingestion, concept loading/coverage, Stooq archive pricing, universe capture, submissions, metrics, weekly reports, API, and Streamlit UI.
- `finlabd` is the daemon entry point and holds the writer connection.
- FastAPI exposes health, universe, and live market endpoints.
- React/Vite is the primary HTTP frontend; Streamlit remains a legacy read-only UI.
- systemd units exist for the daemon and weekday universe snapshot.

### SEC Company Facts PoC ingestion

- Immutable gzip/raw cache paths and SEC User-Agent/rate-limit etiquette are implemented.
- Company Facts JSON is streamed and transformed to reported observations, with duration buckets and restatement-preserving accession/file-date fields.
- Logical fact keys detect duplicates/conflicts and 64-bit hashed keys support stable deduplication.
- The PoC universe loader, entity and filing loading, staging, and warehouse partition publication are implemented.
- Submissions documents backfill accepted timestamps, filing period, primary-document URL, and entity descriptors.

### Warehouse and schema migration framework

- `schema.sql` is used for fresh databases and stamps the highest migration number.
- Existing databases replay forward-only numbered migrations.
- Migration runner transactionality, checksums, baseline/migration discovery, and unknown-provenance refusal are implemented.
- `test_schema_drift.py` compares baseline replay against migration replay.
- Atomic and partition-pruned Parquet publication is implemented.
- Reader connections are in-memory DuckDB connections with warehouse views and do not open the writer database.

### Point-in-time fundamentals

- `fundamentals_asof(as_of)` and `fundamentals_complete_asof(as_of)` are installed by `pit.py`.
- The macro enforces `filed_date <= as_of` and chooses the latest vintage.
- Derived Q4 calculation uses annual minus Q1-Q3 while respecting filing-vintage constraints.
- Mandatory PIT, restatement, instant-fact, writer-lock, and read-path behavior is covered by passing tests.

### Concept normalization and metrics

- Twelve canonical concepts and 40 mappings are declared and validated.
- Concept coverage reporting and the 95% threshold exist.
- A point-in-time metrics module and metric publication are implemented.
- Restatement and canonical concept exploration is implemented in the Streamlit UI.

### Universe snapshots and fixed universe

- Immutable daily symbol-directory snapshots retain the file creation date and can be recaptured safely.
- Security/ticker-history rebuilding, delisting handling, and NASDAQ/other exchange classification exist.
- Daily capture is scheduled for weekdays in both APScheduler and systemd deployment files.

### Queue and daemon foundations

- Atomic job claim, stable job IDs, retry/backoff, three-strike deadlettering, and stale-running recovery exist.
- The daemon serializes handlers behind the single writer lock.
- Universe, submissions, derived rebuild, coverage, and weekly brief handlers exist.
- Queue unit tests pass.

### Weekly and live recommendation engines

- A shared four-gate US equity screen is implemented: earnings yield, annual revenue growth, excess momentum, and maximum drawdown.
- Historical disjoint 13-week windows are descriptive, not claimed as predictive backtests.
- Live and closed-session entry-price handling, split/share-basis checks, stale quote checks, and explicit insufficient-data states exist.
- SEC facts are cached with freshness rules and same-day filing exclusion.
- Alpaca IEX quote/calendar/history adapters implement regular sessions, early closes, paging, and 429 cooldowns.
- Weekly ZIP archives are immutable and contain a report, SEC evidence, and dated prices; retries reuse existing output.

### Frontend

- React 19/TypeScript/Vite application has working universe, market screen, watchlist, demo/live controls, and expandable test evidence.
- Vite proxies `/api/*` to FastAPI.
- The application builds and lints successfully.

## Partially implemented

### M1: bulk/full-market SEC ingestion

- PoC ingestion is complete and deliberately scoped to 54 companies.
- `src/finlab/ingest/edgar.py` is only a placeholder.
- There is no full-companyfacts archive loader or full US universe ingestion path beyond the PoC.
- Submission paging is implemented for 54 PoC entities, but no daily EDGAR discovery handler is registered.

### M2: concept/PIT coverage

- The macro and derived Q4 implementation are substantial and tested.
- Coverage is implemented for the PoC but README and architecture call for top-1000/full-universe coverage, which is not possible in the fixed PoC universe.
- The concept map is narrow: 12 canonical concepts rather than the broader architecture target.
- PIT special-purpose analytics are split between macros and direct `fact` reads in `ui/queries.py`; this weakens the invariant's uniformity.

### M3: prices, corporate actions, and universe

- Universe snapshot and fixed security/ticker-history support are implemented.
- Stooq archive loading for raw OHLCV and a 54-name PoC universe is implemented.
- Automated Stooq acquisition is blocked and explicitly fails closed.
- Alpaca IEX supports live and weekly bars but does not populate the warehouse `price_daily` table.
- `corporate_action` exists in the schema only; no split, dividend, or spinoff ingestion/population is implemented.
- Adjustment factors are handled as three vendor series in the weekly live modules, not reconstructed from persisted corporate-action data.
- Full historical price coverage and PIT-valid security joins are not complete.

### M4: daemon orchestration

- Job queue and reclaim behavior are implemented and tested.
- Scheduler entries exist for universe, prices, EDGAR index, submissions, derived rebuild, refresh, weekly brief, and coverage.
- Registered handlers are incomplete: `prices`, `edgar_index`, and `companyfacts_refresh` have no handler and are removed from APScheduler.
- The universe snapshot scheduler is in fact registered and implemented.
- No filing-fetch/text extraction queue beyond placeholders.
- Daemon's `Pools` implementation is mostly reservation/probe logic and has no text-worker implementation.

### Live and weekly report path

- The live API fetches fresh issuer documents and live market data directly on request rather than exclusively using a precomputed warehouse snapshot.
- It has useful explicit failure behavior and no stale-fallback for SEC, but it is operationally different from the architecture's deterministic L2/L3 presentation path.
- Weekly report generation is a practical rules engine with strong archival/replay tests, but it is not a general company report or valuation system.

## Documented but missing

- Full companyfacts bulk ingest and full EDGAR archive/discovery (`src/finlab/ingest/edgar.py`).
- Complete coverage expansion to the top 1000 and beyond 54 fixed filers.
- Automated reachable daily price provider and recurring warehouse publication.
- `corporate_action` population and PIT reconstruction of adjusted history.
- Filing fetch and section splitting modules.
- Pydantic extraction schemas and cache-first Ollama extraction.
- MLflow extraction evaluation and labelled-eval runner.
- Fact-bundle assembly for reports.
- LLM narrative synthesis from a fact bundle.
- Numeric claim critic and report verification/status gating.
- Valuation methods: reverse DCF, WACC/terminal-growth grid, and peer regression.
- Signal definitions and `signal_value` storage.
- Cross-sectional decile backtests and portfolio simulation.
- Deflated Sharpe, purged CV/embargo, tearsheets, and backtest metrics.
- Portfolio holdings, transactions, cash, orders, rebalancing, or positions.
- Recommendation history storage and general portfolio analytics.
- General FastAPI routes for fundamentals, metrics, reports, backtests, or filings; the API currently serves only health, universe, and live market data.
- Frontend routes for weekly brief, company, or restatement pages; React currently exposes one live-workstation view, while those features remain in Streamlit.

## Broken or failing

- Python baseline is not fully green due to two Streamlit weekly tests.
- The Streamlit blank-checkout test is stale relative to the app's live-market-first navigation.
- The archived-edition test assumes a `Path` in a selectbox but receives a string.
- Stated README test count and `zero skipped` result are stale.
- Scheduled `prices`, `edgar_index`, and `companyfacts_refresh` entries are not executable by the current daemon handler map.
- Automated Stooq acquisition is knowingly blocked; normal `finlab ingest prices` without an archive fails rather than returning data.
- No actual report or text-layer execution path exists, despite all corresponding schema tables.
- Frontend dependencies are built and linted, but the frontend has no test coverage.

## Missing tests

The existing suite is strong for PIT, locking, migrations, queue, warehouse, live-provider decisions, universe, and weekly rules. Missing or weakly covered areas include:

- FastAPI route behavior and HTTP status/error contracts.
- React component, interaction, and API integration tests.
- Full SEC Company Facts bulk/archive ingestion.
- EDGAR filing fetch and submission/page-ingestion edge cases beyond PoC fixtures.
- Corporate-action ingestion and adjusted-price reconstruction.
- Historical ticker validity for recycled tickers and split/security identity.
- End-to-end daemon handler coverage for universe, submissions, rebuild, coverage, and weekly brief.
- Handler behavior for the scheduled-but-unimplemented price, EDGAR, and refresh jobs.
- Valuation, report, extraction, critic, signal, and backtest modules, all currently absent.
- Portfolio/transaction/holding invariants and no-portfolio behavior.
- Provider partial-result and provider-semaphore concurrency under real HTTP failures.
- Cross-process/archive corruption and recovery beyond current fixtures.
- Production read path for all new API routes.

## Architectural risks

1. **PIT invariant uniformity**
   - `ui/queries.py` directly reads `fact` for shares, restatement candidates/history, and concept periods.
   - Restatement access is a special historical analysis, but CLAUDE.md says analytics code must not select `fact` directly and all fundamentals access goes through `fundamentals_asof(as_of)`.

2. **Stale README and architecture synchronization**
   - README figures and test counts are stale. Tests and implementation have advanced, while status prose has not.

3. **Live API timing can diverge from deterministic warehouse records**
   - Live fundamentals are resolved from on-demand raw SEC downloads and live Alpaca data, with their own cache/as-of rules.
   - This is useful for a live workbench but should not become the source of report numbers unless normalized through the fact-bundle/PIT contract.

4. **Share basis and corporate actions are not modeled as-of**
   - Weekly/live code derives splits and distributions from provider bars and fails closed where basis is uncertain.
   - That is safer than inventing an answer, but it does not implement a general PIT-adjustment layer or dividend/spinoff ingestion.

5. **Fixed universe survivorship risk**
   - The 54-company universe is pinned for the PoC and is not a historical survivorship-safe universe.
   - Pre-2026 backtests would be contaminated unless their `backtest_run.caveats` and universe snapshots say so explicitly.

6. **Daemon schedule advertises jobs it cannot execute**
   - Missing handlers are only logged/removed, so operators can infer that scheduled work is active when it is not.

7. **Single-writer invariant needs CI enforcement**
   - Writer lock, reader isolation, and read-path tests are good, but direct DuckDB writes outside `write_conn()` could regress silently without stronger source-level checks.

8. **Pandas remains in the Streamlit path**
   - It is library-required for Streamlit/DuckDB conversion and is explicitly pinned around a pyarrow crash, but it is an avoidable memory and correctness risk for larger screens.

9. **Stooq dependency concentration**
   - Bulk price ingestion currently depends on a manually downloaded archive because automated access is blocked.
   - Live Alpaca IEX keys are also provider credentials and only cover the live/weekly path, not the warehouse layer.

10. **No report provenance enforcement yet**
    - Report, claim, and bundle tables exist, but no composer/critic implementation ensures report numbers come only from `fact_bundle`.

11. **No extraction/LLM cache implementation**
    - Text schema columns and cache-key intent exist, but the source modules are placeholders and no LLM quota is currently consumed.

12. **Security and operational exposure**
    - FastAPI and Streamlit bind through CLI options; deployment examples emphasize loopback. CORS is limited to local Vite origins.
    - Long-running providers and the live API have no formal authentication layer, which is appropriate for loopback PoC use but not public deployment.

## Known limitations

- The project works as a PoC vertical slice on 54 US filers and 12 canonical concepts.
- SEC Company Facts data in the repository has been observed as 1.5M+ observations, but coverage may vary by filing vintage and source availability.
- Stooq history is not reliably auto-downloadable and is absent until a local bulk archive is supplied.
- No general corporate-action table is populated.
- Live/weekly output is provider-dependent and may abstain due to keys, network, stale history, split uncertainty, or incomplete calendar/price sessions.
- Streamlit is present but is not the primary React frontend.
- No valuation, report, extraction, signal, backtest, or portfolio implementation exists.
- The existing universe is fixed and cannot repair historical survivorship bias.
- No frontend tests are present.

## Recommended dependency-aware implementation plan

### Milestone 1 — Full SEC ingestion foundation

**Prerequisite:** stabilize the current failing UI tests and update stale documentation.

Implement:

1. Implement `src/finlab/ingest/edgar.py` with the existing SEC retry/rate-limit client.
2. Add a bulk Company Facts archive loader that reuses `parse_companyfacts` and `load_parsed` unchanged as documented.
3. Replace or supplement the fixed universe ingestion with full archive discovery and explicit ingestion scope metadata.
4. Register `edgar_index` and `companyfacts_refresh` in the daemon.
5. Add full-market/company subset coverage tests and an ingestion run-limit/size test.
6. Preserve the immutable raw cache, fact-key conflict checks, and point-in-time fields.

Exit criteria:

- One command ingests a full Company Facts archive.
- PoC mode remains reproducible.
- Missing/partial archive behavior is explicit.
- Daemon refresh and daily discovery jobs execute.

### Milestone 2 — PIT hardening and canonical data expansion

**Prerequisite:** M1 provides broader fact coverage.

Implement:

1. Remove direct analytics reads from `fact`; route restatement and shares queries through approved PIT or purpose-built historical macros.
2. Add source-level/CI checks that reject direct `fact` reads in analytics modules.
3. Expand canonical concepts and mapping coverage to meet the 95% target on a broader universe.
4. Correct and simplify PIT coverage queries; keep restatement vintages as a deliberately explicit historical view.
5. Add accepted-time PIT rules for intraday filing timing and next-open semantics.

Exit criteria:

- Mandatory PIT test remains green and no analytics module bypasses the macro.
- Concept coverage is measured against a documented universe, not only 54 PoC names.

### Milestone 3 — Prices, corporate actions, and PIT universe

**Prerequisite:** M1 scope and M2 ticker/CIK resolution are stable.

Implement:

1. Add a supported daily warehouse price provider or a robust scheduled local archival/import adapter.
2. Implement `corporate_action` ingestion for splits, dividends, and spinoffs.
3. Derive adjusted history as-of a date from raw OHLCV plus actions, not from vendor adj-close snapshots.
4. Add ticker-history validity joins and security lifecycles to all price/market queries.
5. Populate `price_daily` through `finlabd`; remove the separate live-only bar path as a warehouse dependency.
6. Register and implement the daemon `prices` handler.

Exit criteria:

- Price coverage and corporate-action PIT tests pass.
- No backtest can join by unbounded ticker string.
- Raw and adjusted series are demonstrably distinct and reproducible.

### Milestone 4 — Daemon completion and operational observability

**Prerequisite:** M1-M3 handler implementations exist.

Implement:

1. Implement every scheduled job or remove it from the schedule until implementation exists.
2. Add filing-fetch, section-split, extraction enqueue, rebuild, and refresh handlers as dependencies become ready.
3. Add queue metrics, deadletter diagnostics, and scheduled-job visibility.
4. Exercise every handler in integration tests while holding the writer lock.
5. Add deployment checks for daemon exclusivity and stale universe snapshots.

Exit criteria:

- No schedule entry has an unimplemented handler.
- The daemon can recover after SIGTERM/SIGKILL and does not overlap a CLI writer.

### Milestone 5 — Filing text extraction layer

**Prerequisite:** M1 filings and primary-document URLs; M4 queue handlers.

Implement:

1. Implement filing fetch, gzip/SHA-256 storage, and Item-section splitting.
2. Implement Pydantic extraction schemas and text SHA-256 cache keys.
3. Add Ollama/Pydantic-AI extraction with exactly three concurrent workers and backoff on 429.
4. Enqueue extraction work in the job table with the configured model routing.
5. Add MLflow eval support and run labelled extraction regressions for extraction changes.

Exit criteria:

- Identical text hits the extraction cache regardless of accession/section identity.
- Extraction output is Pydantic-valid and cache-keyed by `text_sha256`.
- M5 extraction changes have a logged MLflow eval run.

### Milestones 6-7 implementation boundary

Do not begin feature implementation until M1-M5 close the data, PIT, scheduler, and text dependencies. M6 should then add valuation, fact-bundle construction, report composition, and critic gating. M7 should add signals, research trials, PIT backtests, deflated/portfolio metrics, and any portfolio capability only after a survivorship-safe universe and action-adjusted price history exist.

Recommended M6 prerequisites:

- M5 extraction and schemas
- M3 raw price/corporate-action history
- M2 complete PIT macros
- A `fact_bundle` builder with provenance

Recommended M7 prerequisites:

- M6 report provenance and deterministic valuation
- A historical PIT universe snapshot
- Raw and adjusted prices
- A completed multiple-testing ledger design

## Post-audit implementation progress

### Yahoo free-provider foundation — started

- Added `MarketDataProvider` protocol and normalized raw OHLCV/corporate-action contracts.
- Added a `YahooProvider` adapter using `yfinance` for daily history, latest quotes, and actions.
- Added `finlab ingest prices-yahoo --start YYYY-MM-DD --end YYYY-MM-DD` and `make prices-yahoo START=... END=...`.
- Added a warehouse loader that maps Yahoo tickers to existing FinLab security IDs and publishes through the existing writer/atomic Parquet path.
- Repaired pytest src-layout resolution and made the blank-checkout Streamlit test green.

Validation status:

- Complete Python suite: green with 185 passing and 4 skipped in the latest full run.
- Provider/loader tests: 5 offline tests passing.
- React production build: passing.
- React lint: passing.
- Backend CLI/provider targeted tests: passing.

Remaining before this foundation is complete:

- Real Yahoo incremental download checkpoints and resume behavior.
- Configurable asset universe instead of the fixed PoC universe.
- End-to-end network smoke test for Yahoo and provider schema failures.
- Corporate actions must be tested against real fixtures and used to derive adjusted prices.
- The existing global Ruff lint still reports 13 pre-existing Streamlit typography/path diagnostics; they have not changed financial behavior.
