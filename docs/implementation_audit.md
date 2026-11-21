# Implementation Audit

**Audit date:** 2026-11-21

## Current architecture

FinLab is undergoing refactoring into two independently executable applications:

- **Data Engine**: writer, providers, SEC ingestion, universe snapshots, queue, Parquet warehouse, snapshot publication.
- **Analytics**: read-only FastAPI/React, PIT queries, factors, scoring, backtests and research reports.

The package root is `src/finlab`. Single-writer invariants remain enforced by `write_conn()` and the OS advisory lock.

## Implemented

- SEC, Stooq, Yahoo and Alpaca adapters exist or are explicitly scoped.
- Point-in-time macros, restatement views, Q4 derivation and metrics are implemented.
- DuckDB/Parquet publication and reader isolation are implemented.
- The Data Engine/Analytics package boundary and snapshot contract exist.
- `finlab-engine` and `finlab-analytics` entry points exist.
- React/Vite and FastAPI are the only application frontend and API surface.
- GitHub Actions runs backend tests/lint and React build/lint.
- Offline market-factor and robust multifactor-scoring modules exist.

## Retired

Streamlit and `src/finlab/ui/` were removed. The Python dependency and CLI/UI launch paths are no longer supported. React/FastAPI is the maintained interface.

## Partially implemented

- A complete S&P 100 universe snapshot is not yet automatically captured.
- Yahoo refresh is scheduled as a short incremental window, without resumable checkpoints.
- SEC full-archive/EDGAR discovery is incomplete.
- Corporate-action reconstruction is not complete.
- Walk-forward backtesting, portfolio holdings and risk contributions remain missing.
- Analytics live mode is not yet backed entirely by the published warehouse snapshot.
- Dataset publication needs a full staged epoch/rollback manifest.

## Missing tests

- Large-universe provider fixtures and rate-limit behavior.
- Corporate-action adjusted-price reconstruction.
- Snapshot checksum verification between epoch generations.
- Cross-sectional ranking across sectors and asset classes.
- Walk-forward/purged backtests and transaction costs.
- Portfolio transactions and scenario analytics.
- React component/API integration tests.

## Validation

- Backend pytest suite: passing.
- Backend Ruff: passing.
- React build: passing.
- React lint: passing.
- GitHub Actions workflow: configured.
