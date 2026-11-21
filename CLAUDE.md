# finlab

Self-hosted financial research platform. Ingests SEC filings and market data,
computes valuations deterministically, generates verified company reports, and
provides a rigorous environment for signal research.

Full design: `docs/architecture.md`. Read it before any non-trivial change.

## Hard invariants — never violate these

1. **Point-in-time.** No query may read a fact whose `filed_date` is later than
   the query's `as_of_date`. All fundamentals access goes through the
   `fundamentals_asof(as_of)` macro. Never `SELECT` from `fact` directly in
   analytics code.

2. **Single DuckDB writer.** Exactly one process (`finlabd`) holds the
   read-write connection to `data/db/finlab.duckdb`. Every other consumer —
   Streamlit, FastAPI, React/Vite, notebooks, and CLI read commands — opens an
   in-memory DuckDB or reads the Parquet warehouse. Never open a second
   read-write connection.

3. **No LLM in the deterministic path.** Ratios, valuations, screens, signals,
   and backtests are SQL and NumPy. LLMs touch only unstructured filing text.
   A number that appears in a report was computed, never generated.

4. **Report numbers come from `fact_bundle`.** The composer receives a JSON
   bundle and may not emit any number absent from it. The critic re-extracts
   every numeric claim and checks it. A report with any `mismatch` or
   `unsupported` claim cannot reach `status='verified'`.

5. **Extraction cache key is `text_sha256`**, not `section_id`. Identical text
   across quarters must hit cache. Never re-extract an immutable filing.

6. **Log every backtest trial.** Insert into `research_trial` *before* running a
   backtest. `backtest_run.n_prior_trials` is read from it at run time and feeds
   the deflated Sharpe ratio.

## Stack — do not substitute without asking

- Python 3.12, `uv` for deps
- DuckDB 1.x + Parquet (extensions: httpfs, vss)
- **Polars** (lazy) for transforms — pandas only where a library requires it
- httpx (async) + tenacity
- selectolax for HTML parsing
- pydantic v2, pydantic-settings, pydantic-ai
- APScheduler (in-process), systemd for supervision
- structlog → JSON
- numpy, scipy, statsmodels, quantstats
- MLflow OSS (file backend) for extraction evals
- FastAPI for the read-only HTTP API
- Streamlit for the legacy read-only UI
- React 19 + TypeScript + Vite for the web frontend
- typer for CLI
- ruff, pytest, pre-commit

## Environment setup

**`uv sync --all-extras` is the development and CI setup.** Use it. A bare
`uv sync` installs only the core dependencies and will leave M5 (`pydantic-ai`,
`mlflow`), M7 (`statsmodels`, `quantstats`) and the UI (`streamlit`) missing.

The extras exist so the base install stays small, not so parts of the stack are
optional: `text`, `eval`, `research`, `ui`. Everything in the stack list above
is declared somewhere; nothing was dropped.

## Explicitly rejected — do not introduce

- Airflow, Dagster, Prefect, Celery, Redis (APScheduler in-process is sufficient)
- Postgres, SQLAlchemy ORM, Alembic (raw SQL + numbered migrations)
- Spark / PySpark (16 GB RAM)
- LangChain (pydantic-ai is the agent layer)
- pandas as the default dataframe (memory)
- Any paid data vendor

## Memory discipline

`memory_limit` is set to 9 GB. Never materialise a full table with `.df()` or
`.pl()`. Aggregate in SQL, or use `pl.scan_parquet()` and collect only the
result. If a function needs more than ~500 MB of Python-side memory, it is
written wrong.

## External API etiquette

- SEC: `User-Agent` header must carry a real contact email. Hard cap 10 req/s.
- Ollama Cloud: `Semaphore(3)`. Map HTTP 429 to `job.not_before = now() +
  backoff`, never to an exception that kills the worker.

## Model routing

| Job | Model |
| --- | --- |
| section classification, boilerplate detection | `gpt-oss:20b` |
| structured field extraction | `gpt-oss:120b` |
| report narrative synthesis | `gpt-oss:120b` |
| critic / numeric verification | `gpt-oss:20b` |

## Testing

- Every module needs unit tests. `pytest` must be green before a session ends.
- **Mandatory regression test:** assert that for a set of random
  `(cik, as_of_date)` pairs, no row returned by `fundamentals_asof` has
  `filed_date > as_of_date`. This test may never be skipped or weakened.
- Extraction changes require a run against the labelled eval set with results
  logged to MLflow.

## Style

- Type hints everywhere; `ruff` clean.
- Raw SQL in `.sql` files or clearly-delimited string constants, not built by
  string concatenation from user input.
- Prefer boring, explicit code over clever code. This system's failure mode is
  silent wrongness, not slowness.

## Schema and migrations

`src/finlab/db/schema.sql` is the canonical baseline: a fresh `finlab db init`
applies it and stamps `schema_version` to the highest migration number present.
`src/finlab/db/migrations/NNNN_*.sql` are forward-only deltas applied to
existing databases by `finlab db migrate`.

When you change the schema you must do **both**: add a numbered migration *and*
update `schema.sql` so the two stay equivalent. `tests/test_schema.py` checks
that a freshly-initialised database contains every table the architecture
document specifies.

## Milestones

M0 scaffold (done) → M1 companyfacts ingestion → M2 concept map + PIT →
M3 prices/universe/metrics → M4 daemon → M5 text layer → M6 reports + critic →
M7 research layer. One milestone per session; each has acceptance criteria that
must pass before the session ends.
