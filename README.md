# finlab

FinLab is a fully local financial research platform split into two applications:

- **Data Engine**: SEC, Yahoo, Stooq and universe ingestion; DuckDB/Parquet warehouse; one writer; snapshot publication.
- **Analytics**: read-only FastAPI and React interface; point-in-time fundamentals, factors, ranking, backtesting and weekly research.

No paid data provider or cloud database is required.

## Quick start

```bash
make setup
make env
# Set FINLAB_SEC_USER_AGENT in .env to a real contact address.

make engine-publish
make engine
make analytics
make web
```

Open:

- React: http://127.0.0.1:5173
- Analytics API: http://127.0.0.1:8000
- OpenAPI: http://127.0.0.1:8000/docs

Stop local listeners:

```bash
make stop
```

## Commands

| Target | Purpose |
|---|---|
| `setup` | Install Python dependencies and initialize DuckDB |
| `engine` | Run the Data Engine daemon; only writer |
| `engine-publish` | Publish a versioned warehouse snapshot |
| `analytics` | Run read-only Analytics FastAPI |
| `analytics-status` | Validate the latest warehouse snapshot |
| `web` | Run React/Vite |
| `app` | Run Analytics and React together |
| `ingest` | Ingest the fixed SEC PoC universe |
| `prices-yahoo` | Backfill Yahoo OHLCV |
| `universe` | Capture the immutable daily universe snapshot |
| `submissions` | Backfill filing submission metadata |
| `concepts` | Load concepts and derive Q4 |
| `metrics` | Recompute metrics |
| `test` | Run backend tests |
| `lint` | Run Ruff checks |
| `frontend-build` | Build and lint React |
| `check` | Run backend and frontend CI checks |

Override dates with `make prices-yahoo START=1996-01-01 END=2026-11-21`.

## Architecture invariants

1. Exactly one process writes DuckDB through `write_conn()`.
2. Analytics never opens a market-data provider or a read-write database connection.
3. Fundamentals use `fund fundamentals_asof(as_of)`-style PIT macros; no future filing may affect an `as_of` result.
4. Raw observations are immutable. Derived data carries provenance and calculation versions.
5. Analytics consumes only a validated published warehouse snapshot.

## Stack

Python 3.12, uv, DuckDB, Parquet, Polars, FastAPI, React 19, TypeScript, Vite, APScheduler, Pydantic, NumPy, SciPy and pytest/Ruff.

Full design: `docs/architecture.md`.
