# finlab

Self-hosted financial research platform. Ingests SEC filings and market data,
computes valuations deterministically, generates verified company reports, and
provides a rigorous environment for signal research.

Design: [`docs/architecture.md`](docs/architecture.md).
Working agreements for contributors (human or agent): [`CLAUDE.md`](CLAUDE.md).

## Setup

```bash
uv sync
cp .env.example .env    # then set FINLAB_SEC_USER_AGENT to a real contact address
uv run finlab db init
uv run finlab db status
```

## Layout

```
src/finlab/
  config.py      settings (pydantic-settings, .env)
  cli.py         typer entrypoint
  db/            connection management, schema, migrations
  ingest/        EDGAR, companyfacts, prices, universe        (M1, M3)
  core/          concept map, PIT layer, metrics, valuation   (M2, M3, M6)
  text/          filing fetch, section split, LLM extraction  (M5)
  report/        fact bundle, composer, critic                (M6)
  research/      signals, backtests, statistics               (M7)
  daemon/        the single-writer process                    (M4)
  ui/            read-only Streamlit app
```

## The two rules that matter most

**Point-in-time.** Fundamentals are read through the `fundamentals_asof(as_of)`
macro, which filters `filed_date <= as_of`. Nothing reads the `fact` table
directly. `tests/test_pit.py` enforces this and may not be weakened.

**One writer.** Exactly one process holds the read-write DuckDB connection.
`finlab.db.conn.write_conn()` takes an OS-level lock to make a second writer
fail loudly rather than corrupt anything. Readers open an in-memory database
over the Parquet warehouse instead.
