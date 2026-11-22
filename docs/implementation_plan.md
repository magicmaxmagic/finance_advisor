# FinLab implementation plan

Continue the existing application; each increment must leave existing tests,
Ruff checks and frontend build green. Use deterministic fixtures for core tests.
Do not expose strategy results before correctness gates pass.

| Order | Increment | Acceptance gate | State |
|---|---|---|---|
| 0 | Source audit and baseline | Document actual modules, risks and baseline | Complete |
| 1a | Immutable publication | Readers pinned across updates; reject tampering | Complete |
| 1b | Incremental recovery | Per-security persisted observation watermarks; correction overlap; cold-start history; partial downloads retry | In progress |
| 1c | Price contract and quality | Explicit adjustment convention, adjusted close, UTC ingestion/provider provenance; reject invalid/duplicate/conflicting bars; migration parity | Pending |
| 1d | Complete publication cycle | Export actions/identity; publish only after complete successful cycle; interrupted-cycle fixture preserves prior epoch | Pending |
| 1e | Daily operation | After-close New York schedule, exchange holiday/session checks, startup catch-up, launchd and systemd, freshness API | Pending |
| 1f | SEC availability | Acceptance timestamps, amendment/restatement fixtures, statement coverage and update handler | Pending |
| 2a | Factors | Correct fixed horizons, benchmark alignment, total returns, risk and technical formulas documented and tested | Pending |
| 2b | Fundamentals and ranking | Canonical PIT queries, units/currency, sector normalization, explicit insufficient status, model version/history; API uses shared modules | Pending |
| 3 | Backtesting | Next-session execution, actions, membership, costs, trial ledger; benchmark and walk-forward separation with hand-calculated fixtures | Pending |
| 4 | Portfolio | Separate transaction store, FIFO accounting, USD/CAD FX history, allocation/risk/scenarios with reconciled fixtures | Pending |
| 5 | Research experience | Existing React views extended, warehouse-only weekly report, archived score changes and outcome evaluation; API/build checks | Pending |
| 6 | Optional local explanations | Ollama consumes structured results only; offline deterministic behavior unchanged | Pending |

## Current increment: 1b

Use the most recent stored observation for each security as the recovery anchor,
not a fixed ten-day window from today. Request a configurable overlap before
that observation to catch recent corrections. Securities without history use a
configurable historical start. Group securities sharing a request window.
A missing symbol must raise a retryable job failure rather than report success;
retained observations make retries idempotent. This does not detect internal
historical gaps, prove session completeness or repair corrections older than
the overlap; those require the session/quality increments above.

## Validation and release discipline

For every increment: targeted correctness fixtures, full pytest, Ruff lint and
format, frontend TypeScript/build/lint, API compatibility where affected, and
updated architecture/status documentation. No automatic real-money orders,
mandatory paid API, cloud infrastructure or mandatory LLM. Do not claim live
provider or scheduling validation from fixture tests. Keep milestone 1 open until
all its acceptance gates pass. Re-evaluate the plan after discovering source or
provider constraints.
