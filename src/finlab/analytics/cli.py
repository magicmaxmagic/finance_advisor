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
    reload: bool = False,
) -> None:
    """Serve read-only Analytics FastAPI routes."""
    uvicorn.run("finlab.api.app:app", host=host, port=port, reload=reload)


@app.command("recommend")
def recommend(as_of: str = typer.Option(..., "--as-of")) -> None:
    """Archive an offline, reproducible historical research report."""
    from datetime import date

    from finlab.analytics.recommendations import archive_recommendations, recommendation_report

    report = recommendation_report(date.fromisoformat(as_of))
    path = archive_recommendations(report, get_settings().reports_dir / "recommendations")
    typer.echo(f"{len(report['rows'])} securities; report: {path}")


@app.command("portfolio")
def portfolio_command(
    transactions_csv: str, as_of: str, base_currency: str = "USD", foreign_fx: float = 0.0
) -> None:
    """Analyze a local transaction CSV using warehouse closes and supplied valuation FX."""
    import json
    from datetime import date
    from pathlib import Path

    from finlab.analytics.portfolio_service import portfolio_from_csv

    report = portfolio_from_csv(
        Path(transactions_csv).read_text(),
        as_of=date.fromisoformat(as_of),
        base_currency=base_currency,
        foreign_fx=foreign_fx if foreign_fx > 0 else None,
    )
    typer.echo(json.dumps(report, indent=2, allow_nan=False))


@app.command("backtest")
def backtest_command(
    start: str, end: str, tickers: str, strategy: str = "equal_weight", top_n: int = 5
) -> None:
    """Evaluate a local historical selection; archive metrics, equity and assumptions."""
    import json
    from datetime import date

    from finlab.research.warehouse_backtest import backtest_warehouse

    result = backtest_warehouse(
        date.fromisoformat(start),
        date.fromisoformat(end),
        tickers.split(","),
        strategy=strategy,
        top_n=top_n,
    )
    path = get_settings().reports_dir / "research" / "latest_backtest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, default=str, allow_nan=False, indent=2) + "\n")
    typer.echo(path)


@app.command("evaluate")
def evaluate_command(report_path: str, horizon: int = 63) -> None:
    """Measure next-open forward outcomes of an archived historical report."""
    import json
    from pathlib import Path

    from finlab.analytics.evaluation import evaluate_recommendations

    report = json.loads(Path(report_path).read_text())
    result = evaluate_recommendations(report, horizon)
    output = (
        get_settings().reports_dir
        / "recommendations"
        / (Path(report_path).stem + f"-forward-{horizon}-{result['evaluation_epoch']}.json")
    )
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    typer.echo(output)


@app.command("walk-forward")
def walk_forward_command(
    start: str, end: str, tickers: str, train: int = 504, validation: int = 126, test: int = 126
) -> None:
    """Select deterministic strategies on validation and evaluate disjoint test folds."""
    import json
    from datetime import date

    from finlab.research.walk_forward import walk_forward

    result = walk_forward(
        date.fromisoformat(start),
        date.fromisoformat(end),
        tickers.split(","),
        train=train,
        validation=validation,
        test=test,
    )
    output = get_settings().reports_dir / "research" / "latest_walk_forward.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, default=str, allow_nan=False, indent=2) + "\n")
    typer.echo(output)


@app.command("weekly")
def weekly_command() -> None:
    """Produce the completed-week research archive using only published inputs."""
    from finlab.research.weekly_report import generate

    typer.echo(generate(get_settings()))


@app.command("research-daemon")
def research_daemon() -> None:
    """Run the independent offline weekly Analytics scheduler."""
    from finlab.analytics.scheduler import main

    main()


@app.command("replay")
def replay_command(report_path: str) -> None:
    """Verify an archived report using its epoch, universe, weights and code hash."""
    import json
    from datetime import date
    from pathlib import Path

    from finlab.analytics.provenance import code_hash, runtime_versions
    from finlab.analytics.recommendations import recommendation_report

    original = json.loads(Path(report_path).read_text())
    if (
        original.get("code_sha256") != code_hash()
        or original.get("runtime_versions") != runtime_versions()
    ):
        raise typer.BadParameter(
            "Archived model source/runtime differs; use its local code bundle and pinned environment"
        )
    replayed = recommendation_report(
        date.fromisoformat(original["as_of"]),
        epoch=original["warehouse_epoch"],
        universe_ids=original["configured_universe_ids"],
        factor_weights=original["model_parameters"]["factor_weights"],
    )
    if replayed != original:
        raise RuntimeError("Archived report did not reproduce exactly")
    typer.echo("Verified exact historical report replay")


if __name__ == "__main__":
    app()
