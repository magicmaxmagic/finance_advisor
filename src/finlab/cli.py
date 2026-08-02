"""The `finlab` command line."""

from __future__ import annotations

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


if __name__ == "__main__":
    app()
