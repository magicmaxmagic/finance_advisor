# Upgrade delivery status — 2026-11-21

## Completed in this change

Milestone 0 audit and baseline checks; first Milestone 1 remediation: immutable
warehouse epochs and pinned warehouse HTTP readers, checksum enforcement,
HTTP 503 for invalid/unpublished data, frontend error handling. No database
schema changes or migrations. Existing HTTP paths and successful payload shapes
are preserved; unpublished mutable warehouses now intentionally return 503.

Modified files:

- docs/technical_audit.md — source-based findings and priorities
- docs/architecture.md — contract v2 and operational limitations
- docs/upgrade_status.md — delivery and commands
- src/finlab/engine/snapshot.py — immutable epoch copies and unique identifiers
- src/finlab/analytics/snapshot.py — validation and pinned read connection
- src/finlab/db/conn.py — explicit warehouse root support
- src/finlab/api/app.py — pinned routes and unavailable response
- frontend/src/App.tsx — check HTTP status and display publication errors
- tests/test_application_boundary.py — four deterministic regressions

## Verification

Before edits: 198 pytest tests passed (7.95s); Ruff lint/format passed;
TypeScript/Vite production build and oxlint passed.
After edits: 202 pytest tests passed (7.42s); Ruff lint passed; 90 Python files
passed formatting; TypeScript/Vite build and oxlint passed; git diff --check passed.
No live provider ingestion, browser visual QA, production warehouse publication,
macOS service installation or remote repository comparison was performed.
Python has no configured static type checker; frontend TypeScript was checked.

## Local commands

Run from the repository root. Initial ingestion requires network and a real SEC
contact in `.env`. Keep the daemon stopped during writer CLI operations.

```bash
make setup
make env
# Edit FINLAB_SEC_USER_AGENT in .env before SEC ingestion.
make ingest
make universe
make submissions
make concepts
make prices-yahoo START=2006-11-21 END=2026-11-21
make metrics AS_OF=2026-11-21
make engine-publish
```

Run the two applications independently in separate terminals:

```bash
uv run --no-sync finlabd
uv run --no-sync finlab-analytics serve --host 127.0.0.1 --port 8000
cd frontend && npm run dev -- --host 127.0.0.1 --port 5173
```

`make start` starts all three processes. API: http://127.0.0.1:8000/docs;
frontend: http://127.0.0.1:5173. `make stop` stops them.

Manual bounded price synchronization (replace dates with the correction window):

```bash
make prices-yahoo START=2026-11-10 END=2026-11-21
make engine-publish
```

This is a bounded download/merge, not a verified complete daily synchronization
workflow. The daemon currently downloads a ten-day window; long outage catch-up,
automatic complete epoch publication, holiday gating and launchd remain open.
Do not infer full daily reliability from these commands.

Existing weekly workflow with supplied CSV and local SEC facts:

```bash
uv run --no-sync finlab weekly --prices-csv /absolute/path/to/prices.csv
```

The CSV contract is defined by `src/finlab/ingest/weekly_prices.py`. Without CSV,
the existing command uses Alpaca credentials. It is not yet a warehouse-only
weekly Analytics workflow. No subsequent recommendation performance claim has
been validated.

## Remaining milestones and debt

Milestone 1 is partial. Corporate-action/adjustment contracts, ingestion metadata,
quality gates, incremental watermarks, historical correction recovery, holiday
scheduling and launchd are outstanding. Published epochs use disk copies and
per-request checksum scans; retention and efficient integrity caching are open.
Legacy research readers still use working data. Required dataset presence does
not prove valid prices, complete fundamentals or sufficient benchmark coverage.

Milestones 2–6 are not completed by this change. Existing factor and API ranking
correctness defects are itemized in the audit. Backtesting/statistics/signals are
stubs; portfolio accounting and scenarios are absent. Weekly research needs
warehouse isolation and outcome tracking. No Ollama integration was added.
The full requested definition of done has not been met.

## Continuation: recovery planning

Added `docs/implementation_plan.md` with ordered increments and acceptance gates.
Implemented per-security price recovery in `ingest/price_sync.py`; wired it into
the daemon and a new `finlab-engine sync-prices` command. Configuration lives in
`config.py` and `.env.example`. Action downloads now precede price persistence.
Five fixture tests cover long outages, grouped windows, history bounds,
idempotent retries, missing-symbol recovery and action download failures.

```bash
# Stop finlabd before manual writer commands.
uv run --no-sync finlab-engine sync-prices
uv run --no-sync finlab-engine publish
```

These commands supersede the fixed-window manual price synchronization example
above. The daemon now uses persisted watermarks rather than a fixed ten-day
window from today. Complete daily synchronization still requires the remaining
Milestone 1 gates. No live financial download was run during this continuation.

Continuation validation: 207 pytest tests passed (12.22s), Ruff lint/format
passed (94 Python files), frontend TypeScript/Vite build and oxlint passed.
Four warnings report the existing DuckDB fetch_arrow_table deprecation in the
price loader. No live-provider or installed-service validation was performed.

## Correctness continuation

Added ingestion OHLCV/date/provenance validation, duplicate rejection, provider
mixing rejection and refusal of auto_adjust=True in the Yahoo OHLC loader.
Provider values must be finite. Removed the loader's deprecated Arrow call.
Added deterministic invalid-bar fixtures.

Corrected fixed market-factor horizons, skip-month momentum, benchmark endpoint
checks, MACD line and flat-series RSI. Scoring now uses the true even-sample
median, rejects duplicate vintages and leaves incomplete overall scores null.
Ranking API uses shared scoring, sample volatility, 253 observations and canonical
PIT revenue/EPS. Frontend supports null scores. API as_of now uses FastAPI date
validation. New diluted EPS concept requires a concept refresh and republication.
See factor_methodology.md for formulas and remaining statistical limitations.

Milestone 1 and Milestone 2 remain partial. No backtest/portfolio implementation,
complete exchange scheduling, action-aware return series or statistically validated
recommendation system was added. Existing published price conventions have not
been automatically reinterpreted or migrated.

Final continuation checks: 219 backend tests passed; Ruff lint/format passed
(97 Python files), frontend TypeScript/Vite build and oxlint passed, diff whitespace
check passed. Updated concept inventory test for the new EPS mapping. Q4
subtraction explicitly excludes diluted EPS because per-share values are not
additive when weighted share counts change. Live ingestion remains unverified.
