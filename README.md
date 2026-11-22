# FinLab

FinLab is a local, self-hosted two-application financial research platform:

- **Data Engine**: SEC filing data, Yahoo/Stooq/Alpaca price adapters, universe snapshots, DuckDB/Parquet publication and the only writer process.
- **Analytics**: read-only FastAPI and React interface for warehouse status, point-in-time facts, market factors, explainable rankings and company analysis.

No paid market-data provider or cloud database is required.

## Quick start

```bash
make setup
npm --prefix frontend ci
make env
# Set FINLAB_SEC_USER_AGENT to a real contact address in .env.
make ingest
make universe
make prices-yahoo
env PYTHONPATH=src uv run --no-sync python -m finlab.engine.cli prepare
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
| `ingest` | Ingest the configured SEC research universe |
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

## Research workspace

Start in Overview for the benchmark, publication status and research candidates.
Discover supports a sortable V2 screener and a separate legacy ranking. Company
Research shows price history, score evidence and filing details. Compare keeps up
to four companies at the same cutoff. The guided Portfolio ledger supports buy
and sell entry and optional CSV import. Model Validation, Strategies, Watchlist and
Data Center have separate destinations.

The opportunity score summarizes relative evidence. The historical research
reports disclose uncertainty, coverage and survivorship bias.

- [Current architecture and Mermaid diagrams](docs/architecture.md)
- [Source modules and HTTP endpoints](docs/code_guide.md)
- [Scoring V2 formulas and results](docs/scoring_v2.md)
- [V3 validation methodology and results](docs/validation_v3.md)
- [Frontend design, run commands and verification](frontend/README.md)
- [V4 product guide and evidence policy](docs/product_v4.md)
- [V4 UX audit](docs/ux_v4_audit.md)

## Architecture invariants

1. Exactly one process writes DuckDB through `write_conn()`.
2. Analytics never calls a market-data provider or opens the DuckDB writer.
3. Fundamentals are resolved through point-in-time macros.
4. Raw observations remain immutable; derived observations carry provenance and calculation versions.
5. Analytics should consume a published and validated warehouse snapshot.

## Stack

Python 3.12, uv, DuckDB, Parquet, Polars, FastAPI, React 19, TypeScript, Vite, APScheduler, Pydantic, NumPy, pytest and Ruff.

Interactive API schemas are at `http://127.0.0.1:8000/docs`.
