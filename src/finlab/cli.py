"""The `finlab` command line."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

import duckdb
import typer
from rich.console import Console
from rich.table import Table

from finlab import __version__
from finlab.config import get_settings
from finlab.db.conn import (
    DatabaseMissing,
    WriterLockHeld,
    attach_warehouse_views,
    open_db_read_only,
    read_conn,
    write_conn,
)
from finlab.db.migrations import BASELINE_NAME, current_version, init_database

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="finlab — self-hosted financial research platform.",
)
db_app = typer.Typer(no_args_is_help=True, help="Database administration.")
app.add_typer(db_app, name="db")

ingest_app = typer.Typer(no_args_is_help=True, help="Ingest external sources.")
app.add_typer(ingest_app, name="ingest")

concepts_app = typer.Typer(no_args_is_help=True, help="Canonical concept mapping.")
app.add_typer(concepts_app, name="concepts")

console = Console()
err_console = Console(stderr=True)


def _fail(message: str) -> typer.Exit:
    err_console.print(f"[bold red]error:[/] {message}")
    return typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def main(
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    """Without a subcommand this only handles `--version`; `no_args_is_help`
    covers the bare `finlab` case."""
    if version:
        console.print(f"finlab {__version__}")
        raise typer.Exit()


@db_app.command("init")
def db_init() -> None:
    """Create the data tree and bring the database up to the current schema."""
    cfg = get_settings()
    cfg.ensure_dirs()

    try:
        with write_conn(cfg) as con:
            result = init_database(con)
    except WriterLockHeld as exc:
        raise _fail(str(exc)) from exc

    if result.baseline_applied:
        console.print(f"applied [bold]{BASELINE_NAME}[/] → schema version {result.version}")
    elif result.migrations_applied:
        for migration in result.migrations_applied:
            console.print(f"applied [bold]{migration.version:04d}_{migration.name}[/]")
        console.print(f"schema version {result.version}")
    else:
        console.print(f"already up to date at schema version {result.version}")
    console.print(f"database: {cfg.db_path}")


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply any migrations newer than the database's current version."""
    cfg = get_settings()
    try:
        with write_conn(cfg) as con:
            result = init_database(con)
    except WriterLockHeld as exc:
        raise _fail(str(exc)) from exc

    if result.migrations_applied:
        for migration in result.migrations_applied:
            console.print(f"applied [bold]{migration.version:04d}_{migration.name}[/]")
    else:
        console.print("no pending migrations")
    console.print(f"schema version {result.version}")


@db_app.command("status")
def db_status() -> None:
    """Print schema version, table names and row counts."""
    cfg = get_settings()

    try:
        with open_db_read_only(cfg) as con:
            version = current_version(con)
            names = [
                row[0]
                for row in con.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' ORDER BY table_name"
                ).fetchall()
            ]
            counts = {
                name: con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in names
            }
    except DatabaseMissing as exc:
        raise _fail(str(exc)) from exc
    except duckdb.IOException as exc:
        raise _fail(
            f"cannot open {cfg.db_path} read-only — the daemon probably holds it "
            f"read-write. Stop finlabd, or query the Parquet warehouse instead.\n{exc}"
        ) from exc

    table = Table(title=f"schema version {version}")
    table.add_column("table", style="cyan", no_wrap=True)
    table.add_column("rows", justify="right")
    for name in names:
        table.add_row(name, f"{counts[name]:,}")
    console.print(table)
    console.print(f"database: {cfg.db_path}")

    with read_conn(cfg) as con:
        attached = attach_warehouse_views(con, cfg)
    if attached:
        console.print(f"warehouse parquet present for: {', '.join(attached)}")
    else:
        console.print("warehouse parquet: none yet (ingestion lands in M1)")


@ingest_app.command("poc")
def ingest_poc_command(
    force: Annotated[
        bool, typer.Option("--force", help="Re-download even if the raw file is cached.")
    ] = False,
) -> None:
    """Ingest companyfacts for the proof-of-concept universe."""
    from finlab.ingest.companyfacts import IngestError, ingest_poc
    from finlab.ingest.sec import SecEtiquetteError

    cfg = get_settings()
    cfg.ensure_dirs()

    try:
        with write_conn(cfg) as con:
            report = ingest_poc(con, cfg, force=force)
    except (WriterLockHeld, SecEtiquetteError, IngestError) as exc:
        raise _fail(str(exc)) from exc

    fetch, load = report.fetch, report.load
    console.print(
        f"fetched {len(fetch.downloaded)} · cached {len(fetch.cached)} · "
        f"missing {len(fetch.missing)}"
    )
    if fetch.missing:
        err_console.print(f"[yellow]no companyfacts for CIKs:[/] {sorted(fetch.missing)}")

    console.print(
        f"parsed {load.parse.observations:,} observations → {load.staged_rows:,} rows "
        f"({load.distinct_keys:,} distinct fact_keys)"
    )
    skipped = (
        load.parse.skipped_missing_field
        + load.parse.skipped_null_value
        + load.parse.skipped_bad_date
    )
    if skipped:
        console.print(
            f"skipped {skipped:,} observations "
            f"(missing field {load.parse.skipped_missing_field:,}, "
            f"null value {load.parse.skipped_null_value:,}, "
            f"bad date {load.parse.skipped_bad_date:,})"
        )
    console.print(
        f"fact_key conflicts: {load.conflicting_keys:,} keys / "
        f"{load.conflicting_rows:,} rows ({load.conflict_fraction:.4%})"
    )
    if load.write is not None:
        console.print(
            f"wrote {load.write.rows:,} rows across {load.write.partition_count} partitions"
            + (f", pruned {len(load.write.pruned)}" if load.write.pruned else "")
        )
    console.print(f"entity {load.entities:,} · filing {load.filings:,}")
    console.print(f"peak RSS {report.peak_rss_mb:,.0f} MiB")


@concepts_app.command("load")
def concepts_load() -> None:
    """Load the concept map from YAML, then rebuild the derived Q4 table."""
    from finlab.core.concepts import ConceptMapError, load_concept_map
    from finlab.core.pit import build_q4_table
    from finlab.db.conn import read_conn
    from finlab.ingest.warehouse import publish_table

    cfg = get_settings()
    cfg.ensure_dirs()

    try:
        with write_conn(cfg) as con:
            mappings = load_concept_map(con)
            publish_table(
                con,
                source="concept_map",
                root=cfg.warehouse_dir / "concept_map",
                order_by=("canonical", "priority"),
            )
            console.print(f"loaded {mappings} tag mappings")

            # Q4 derivation reads `fact`, which lives in the Parquet warehouse
            # rather than the DuckDB file, so it is computed on a reader
            # connection. The write lock is still held, which serialises it
            # against any other writer.
            with read_conn(cfg) as reader:
                rows = build_q4_table(reader)
                publish_table(
                    reader,
                    source="fundamentals_q4",
                    root=cfg.warehouse_dir / "fundamentals_q4",
                    order_by=("cik", "canonical", "period_end"),
                )
            console.print(f"derived {rows:,} Q4 rows")
    except (WriterLockHeld, ConceptMapError) as exc:
        raise _fail(str(exc)) from exc


@concepts_app.command("coverage")
def concepts_coverage(
    as_of: Annotated[
        str, typer.Option("--as-of", help="Point-in-time date, YYYY-MM-DD.")
    ] = date.today().isoformat(),
    since: Annotated[
        str, typer.Option("--since", help="Ignore periods ending before this date.")
    ] = "2015-01-01",
) -> None:
    """Report the resolution rate of each canonical concept. Exits 1 below 95%."""
    from finlab.core.concepts import COVERAGE_THRESHOLD, coverage
    from finlab.core.pit import analytics_conn

    cfg = get_settings()
    with analytics_conn(cfg) as con:
        rows = coverage(con, date.fromisoformat(as_of), since=date.fromisoformat(since))

    table = Table(title=f"concept coverage as of {as_of} (periods ending ≥ {since})")
    table.add_column("concept", style="cyan", no_wrap=True)
    table.add_column("resolved", justify="right")
    table.add_column("expected", justify="right")
    table.add_column("coverage", justify="right")
    for row in rows:
        style = "green" if row.passing else "red"
        table.add_row(
            row.canonical,
            f"{row.resolved:,}",
            f"{row.expected:,}",
            f"[{style}]{row.fraction:.1%}[/]",
        )
    console.print(table)

    failing = [r for r in rows if not r.passing]
    if failing:
        raise _fail(
            f"{len(failing)} concept(s) below {COVERAGE_THRESHOLD:.0%}: "
            + ", ".join(f"{r.canonical} ({r.fraction:.1%})" for r in failing)
        )
    console.print(f"[green]all {len(rows)} concepts at or above {COVERAGE_THRESHOLD:.0%}[/]")


@ingest_app.command("prices")
def ingest_prices_command(
    archive: Annotated[
        Path | None,
        typer.Option("--archive", help="Path to a downloaded Stooq d_us_txt.zip."),
    ] = None,
) -> None:
    """Load daily OHLCV for the PoC universe from a Stooq bulk archive."""
    from finlab.ingest.prices import PriceSourceBlocked, fetch_stooq_bulk, load_stooq_archive

    cfg = get_settings()
    cfg.ensure_dirs()

    try:
        target = archive
        if target is None:
            # Attempt the documented source; it currently answers with an
            # anti-bot challenge, which this raises on rather than defeats.
            fetch_stooq_bulk(cfg)
            raise _fail("unreachable: fetch_stooq_bulk returned without raising")
        with write_conn(cfg) as con:
            report = load_stooq_archive(con, cfg, target)
    except (PriceSourceBlocked, FileNotFoundError, WriterLockHeld) as exc:
        raise _fail(str(exc)) from exc

    console.print(f"loaded {report.rows:,} price rows for {len(report.tickers_found)} tickers")
    console.print(f"securities {report.securities}")
    if report.tickers_missing:
        err_console.print(f"[yellow]no price series for:[/] {', '.join(report.tickers_missing)}")


@app.command("metrics")
def metrics_build(
    as_of: Annotated[
        str, typer.Option("--as-of", help="Point-in-time date, YYYY-MM-DD.")
    ] = date.today().isoformat(),
) -> None:
    """Compute the ratio set at a point in time and store it in `metric`."""
    from finlab.core.metrics import compute_metrics, install_metric_macros, store_metrics
    from finlab.core.pit import analytics_conn

    cfg = get_settings()
    when = date.fromisoformat(as_of)

    with analytics_conn(cfg) as reader:
        install_metric_macros(reader)
        frame = compute_metrics(reader, when)

    non_null = int(frame.get_column("value").is_not_null().sum())
    try:
        with write_conn(cfg) as con:
            stored = store_metrics(con, frame, when)
            from finlab.ingest.warehouse import publish_table

            publish_table(
                con,
                source="metric",
                root=cfg.warehouse_dir / "metric",
                order_by=("cik", "as_of_date", "metric_name"),
            )
    except WriterLockHeld as exc:
        raise _fail(str(exc)) from exc

    console.print(
        f"computed {len(frame):,} metric rows ({non_null:,} non-null) as of {as_of}; "
        f"{stored:,} stored"
    )


@app.command("ui")
def ui(
    port: Annotated[int, typer.Option("--port", help="Port to bind.")] = 8501,
    host: Annotated[
        str, typer.Option("--host", help="Interface to bind. Loopback by default.")
    ] = "127.0.0.1",
) -> None:
    """Launch the read-only Streamlit app.

    Bound to loopback: reach it over an SSH tunnel rather than exposing it.
    """
    import subprocess
    import sys

    from finlab.ui import app as ui_app

    raise typer.Exit(
        subprocess.call(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(Path(ui_app.__file__)),
                "--server.address",
                host,
                "--server.port",
                str(port),
                "--server.headless",
                "true",
            ]
        )
    )


if __name__ == "__main__":
    app()
