# FinLab

FinLab is a local, self-hosted two-application financial research platform:

- **Data Engine**: SEC filing data, Yahoo/Stooq/Alpaca price adapters, universe snapshots, DuckDB/Parquet publication and the only writer process.
- **Analytics**: read-only FastAPI and React interface for warehouse status, point-in-time facts, market factors, explainable rankings and company analysis.

No paid market-data provider or cloud database is required.

## Quick start

```bash
make setup
make env
# Set FINLAB_SEC_USER_AGENT to a real contact address in .env.
make ingest
make universe
make prices-yahoo
make start
make status
```

Open:

- React: http://127.0.0.1:5173
- Analytics API: http://127.0.0.1:8000
- OpenAPI: http://127.0.0.1:8000/docs

Stop all local services:

```bash
make stop
```

## Main commands

| Target | Purpose |
|---|---|
| `setup` | Install Python dependencies and initialize DuckDB |
| `start` | Start Data Engine, Analytics API and React |
| `status` | Show PIDs and active listeners |
| `stop` | Stop all local FinLab services |
| `ingest` | Ingest the fixed SEC PoC universe |
| `prices-yahoo` | Backfill daily Yahoo OHLCV |
| `universe` | Capture one immutable universe snapshot |
| `submissions` | Backfill SEC filing metadata |
| `concepts` | Load concept mappings and derive Q4 |
| `metrics` | Recompute derived metrics |
| `engine-publish` | Publish a versioned warehouse snapshot |
| `test` | Run backend tests |
| `lint` | Run Ruff lint and format checks |
| `frontend-build` | Build and lint React |
| `check` | Run backend and frontend CI-style validation |

Override Yahoo dates with:

```bash
make prices-yahoo START=1996-01-01 END=2026-11-21
```

## Analytics views

- **Overview**: warehouse and methodology summary.
- **Ranking**: real descriptive ranking using earnings yield, momentum, volatility and maximum drawdown.
- **Company**: selected issuer, latest filing, recent SEC facts and historical closes.
- **Data**: warehouse counts and date coverage.

Scores are descriptive research outputs, not guarantees of profit or unsupported buy/sell recommendations.

## API

- `GET /api/health`
- `GET /api/universe`
- `GET /api/warehouse/status`
- `GET /api/warehouse/assets/{ticker}`
- `GET /api/analytics/rankings?as_of=YYYY-MM-DD`

## Architecture invariants

1. Exactly one process writes DuckDB through `write_conn()`.
2. Analytics never calls a market-data provider or opens the DuckDB writer.
3. Fundamentals are resolved through point-in-time macros.
4. Raw observations remain immutable; derived observations carry provenance and calculation versions.
5. Analytics should consume a published and validated warehouse snapshot.

## Stack

Python 3.12, uv, DuckDB, Parquet, Polars, FastAPI, React 19, TypeScript, Vite, APScheduler, Pydantic, NumPy, pytest and Ruff.

Full design: `docs/architecture.md`.
