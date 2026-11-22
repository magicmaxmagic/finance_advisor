"""Reproducible local research candidates from historical warehouse observations."""

import hashlib
import json
from datetime import date
from pathlib import Path

from finlab.analytics.provenance import archive_sources, code_hash, runtime_versions
from finlab.analytics.rankings import historical_rankings
from finlab.analytics.research_context import market_context


def recommendation_report(
    as_of: date,
    *,
    epoch: str | None = None,
    universe_ids: list[str] | None = None,
    factor_weights: dict[str, float] | None = None,
    settings=None,
) -> dict:
    ranking = historical_rankings(
        as_of,
        epoch=epoch,
        universe_ids=universe_ids,
        factor_weights=factor_weights,
        settings=settings,
    )
    ranking["code_sha256"] = code_hash()
    ranking["runtime_versions"] = runtime_versions()
    ranking["market_overview"] = market_context(
        as_of, ranking.get("warehouse_epoch"), settings=settings
    )
    for row in ranking["rows"]:
        components = row.get("factor_scores", {})
        row["positive_signals"] = [
            key for key, value in components.items() if value is not None and value > 55
        ]
        row["negative_signals"] = [
            key for key, value in components.items() if value is not None and value < 45
        ]
        row["explanation"] = (
            "Weighted cross-sectional annual earnings yield, net margin, revenue growth, 12-month price momentum and volatility rank."
            if row["score"] is not None
            else "Required historical price or public fundamental inputs are missing."
        )
        row["risk_assessment"] = {
            "annualized_volatility": row["volatility"],
            "max_drawdown": row["max_drawdown"],
        }
    ranking["limitations"] = [
        "Research ranking is not a probability of profit or a trade instruction.",
        "Historical selection uses the available security catalogue; survivorship bias remains.",
        "Ranking momentum uses close price returns and excludes reinvested dividends. Historical earnings yield requires covered split normalization; unsupported share classes remain unavailable.",
        "No statistically validated backtest supports profitability claims.",
    ]
    return ranking


def archive_recommendations(report: dict, directory: Path) -> Path:
    archive_sources(directory, report["code_sha256"])
    payload = json.dumps(report, sort_keys=True, allow_nan=False, indent=2) + "\n"
    digest = hashlib.sha256(payload.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{report['as_of']}-{digest[:16]}.json"
    if not target.exists():
        temporary = target.with_suffix(".tmp")
        temporary.write_text(payload)
        temporary.replace(target)
    return target
