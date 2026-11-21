"""Standalone Analytics read-only entry point."""

from __future__ import annotations

import typer
import uvicorn

from finlab.analytics.snapshot import SnapshotUnavailable, require_valid_snapshot
from finlab.config import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("status")
def status() -> None:
    """Validate and print the current warehouse snapshot contract."""
    settings = get_settings()
    try:
        snapshot = require_valid_snapshot(settings.warehouse_dir)
    except SnapshotUnavailable as exc:
        raise typer.Exit(str(exc)) from exc
    for key, value in snapshot.items():
        typer.echo(f"{key}={value}")


@app.command("serve")
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Serve read-only Analytics FastAPI routes."""
    uvicorn.run("finlab.api.app:app", host=host, port=port, reload=False)
