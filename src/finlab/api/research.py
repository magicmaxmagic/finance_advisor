"""Local research requests; derived results may be persisted separately."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api")


@router.get("/analytics/model-validation")
def model_validation():
    """Read the latest archived study without running a simulation."""
    from finlab.analytics.validation_view import read_validation, summarize_validation
    from finlab.config import get_settings

    report = read_validation(get_settings().reports_dir / "research")
    if report is None:
        return {"reason": "No historical validation report has been archived."}
    return summarize_validation(report)


@router.get("/analytics/model-validation/series")
def model_validation_series(
    horizon: Literal["21", "63", "126", "252"] = "21", series: str = "opportunity"
):
    """Return the archived time series for one factor or model."""
    from finlab.analytics.validation_view import read_validation, validation_series
    from finlab.config import get_settings

    report = read_validation(get_settings().reports_dir / "research")
    if report is None:
        return {
            "reason": "No historical validation report has been archived.",
            "windows": [],
            "portfolio": [],
        }
    summary = report.get("summary", {}).get(str(horizon), {})
    if series not in (summary.get("models", {}) | summary.get("factors", {})):
        raise HTTPException(
            status_code=404, detail="This series is not in the active research archive."
        )
    return validation_series(report, int(horizon), series)


class StrategyRequest(BaseModel):
    start: date
    end: date
    tickers: list[str] = Field(min_length=1, max_length=100)
    strategy: Literal["equal_weight", "buy_hold", "momentum", "value", "quality", "multifactor"] = (
        "equal_weight"
    )
    top_n: int = Field(default=5, ge=1, le=100)


@router.post("/analytics/backtest")
def backtest(request: StrategyRequest):
    from finlab.research.warehouse_backtest import backtest_warehouse

    try:
        return backtest_warehouse(
            request.start,
            request.end,
            request.tickers,
            strategy=request.strategy,
            top_n=request.top_n,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class PortfolioRequest(BaseModel):
    csv_data: str = Field(min_length=1, max_length=1000000)
    as_of: date
    base_currency: Literal["USD", "CAD"] = "USD"
    foreign_fx: float | None = Field(default=None, gt=0)
    scenario_security: str | None = None
    scenario_amount: float | None = Field(default=None, gt=0)


@router.post("/portfolio/analyze")
def portfolio(request: PortfolioRequest):
    from finlab.analytics.portfolio_service import portfolio_from_csv

    try:
        return portfolio_from_csv(
            request.csv_data,
            as_of=request.as_of,
            base_currency=request.base_currency,
            foreign_fx=request.foreign_fx,
            scenario_security=request.scenario_security,
            scenario_amount=request.scenario_amount,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
