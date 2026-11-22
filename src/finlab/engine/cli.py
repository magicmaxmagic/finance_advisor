"""Standalone Data Engine entry point."""

from __future__ import annotations

import typer

from finlab.config import get_settings
from finlab.db.conn import WriterLockHeld, write_conn
from finlab.db.migrations import current_version
from finlab.engine.snapshot import publish_snapshot

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("status")
def status() -> None:
    """Show schema and the latest published warehouse snapshot."""
    from finlab.engine.snapshot import read_latest_snapshot

    settings = get_settings()
    snapshot = read_latest_snapshot(settings.warehouse_dir)
    if snapshot is None:
        typer.echo("No published warehouse snapshot")
    else:
        typer.echo(f"epoch={snapshot.epoch}")
        typer.echo(f"published_at={snapshot.published_at}")
        typer.echo(f"contract={snapshot.contract_version}")


@app.command("publish")
def publish() -> None:
    """Publish a validated warehouse snapshot under the writer lock."""
    settings = get_settings()
    settings.ensure_dirs()
    try:
        with write_conn(settings) as con:
            version = current_version(con)
            snapshot = publish_snapshot(
                warehouse_dir=settings.warehouse_dir,
                schema_version=version,
            )
    except WriterLockHeld as exc:
        raise typer.Exit(str(exc)) from exc
    typer.echo(f"published epoch={snapshot.epoch} contract={snapshot.contract_version}")


@app.command("sync-prices")
def sync_prices_command() -> None:
    """Recover missing price tails with a recent-correction overlap."""
    from datetime import date

    from finlab.ingest.price_sync import sync_prices
    from finlab.ingest.yahoo import YahooProvider

    settings = get_settings()
    with write_conn(settings) as con:
        reports = sync_prices(con, settings, YahooProvider(), end=date.today())
    typer.echo(f"Synchronized {len(reports)} request windows; publication is separate.")


@app.command("sync")
def sync(full: bool = False) -> None:
    """Synchronize filings, prices and actions, then publish one complete epoch."""
    from finlab.daemon.main import handle_prices_yahoo

    settings = get_settings()
    with write_conn(settings) as con:
        typer.echo(handle_prices_yahoo(con, settings, {"full": full}))


@app.command("prepare")
def prepare() -> None:
    """Build canonical fundamentals and publish existing local history offline."""
    from datetime import date

    from finlab.engine.prepare import prepare_warehouse

    cfg = get_settings()
    with write_conn(cfg) as con:
        snapshot = prepare_warehouse(con, cfg, date.today())
    typer.echo(f"Prepared and published {snapshot.epoch}")


if __name__ == "__main__":
    app()
