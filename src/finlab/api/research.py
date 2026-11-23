"""Local research requests; derived results may be persisted separately."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from finlab.analytics.portfolio_ledger import LedgerUpdate

router = APIRouter(prefix="/api")


@router.get("/analytics/calendar")
def calendar(
    start: date,
    end: date,
    as_of: date | None = None,
    ticker: Annotated[list[str] | None, Query()] = None,
    kind: Literal[
        "earnings_release",
        "earnings_call",
        "sec_filing",
        "investor_event",
        "dividend",
        "corporate_action",
    ]
    | None = None,
):
    from finlab.analytics.event_calendar import event_calendar

    try:
        return event_calendar(start, end, as_of or date.today(), ticker or (), kind)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/analytics/model-validation")
def model_validation(study: Literal["v3", "v6", "v7", "v8"] = "v3"):
    """Read the latest archived study without running a simulation."""
    from finlab.analytics.validation_view import read_validation, summarize_validation
    from finlab.config import get_settings

    directory = get_settings().reports_dir / "research"
    report = read_validation(directory, holdout=study == "v6", study=study)
    if report is None:
        return {"reason": "No historical validation report has been archived."}
    return summarize_validation(report)


@router.get("/analytics/model-validation/series")
def model_validation_series(
    horizon: Literal["21", "63", "126", "252"] = "21",
    series: str = "opportunity",
    study: Literal["v3", "v6", "v7", "v8"] = "v3",
):
    """Return the archived time series for one factor or model."""
    from finlab.analytics.validation_view import read_validation, validation_series
    from finlab.config import get_settings

    directory = get_settings().reports_dir / "research"
    report = read_validation(directory, holdout=study == "v6", study=study)
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


@router.get("/portfolio/ledger")
def portfolio_ledger(revision: Annotated[int | None, Query(ge=0)] = None):
    from finlab.analytics.portfolio_ledger import read_ledger
    from finlab.config import get_settings

    record = read_ledger(get_settings().data_dir / "personal" / "portfolio.sqlite", revision)
    if record is None:
        raise HTTPException(status_code=404, detail="No such portfolio revision.")
    return record


@router.put("/portfolio/ledger")
def update_portfolio_ledger(update: LedgerUpdate):
    from finlab.analytics.portfolio_ledger import LedgerConflict, save_ledger
    from finlab.config import get_settings

    try:
        return save_ledger(get_settings().data_dir / "personal" / "portfolio.sqlite", update)
    except LedgerConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
