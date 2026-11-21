"""FastAPI read-only backend for the live market dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from finlab.ingest.universe import load_poc_universe
from finlab.research.live import assess_live
from finlab.research.live_demo import demo_inputs
from finlab.research.weekly import completed_friday

app = FastAPI(title="Finlab API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "finlab-api"}


@app.get("/api/universe")
def universe() -> dict:
    companies = load_poc_universe()
    sectors = sorted({company.sector for company in companies})
    return {
        "companies": [
            {
                "cik": company.cik,
                "ticker": company.ticker,
                "name": company.name,
                "sector": company.sector,
            }
            for company in companies
        ],
        "sectors": sectors,
    }


@app.get("/api/market/live")
def market_live(
    tickers: Annotated[list[str] | None, Query()] = None,
    demo: bool = False,
    demo_price: Annotated[float, Query(gt=0)] = 99.0,
) -> dict:
    """Serve local Analytics output only; external providers belong to the Data Engine."""
    if not demo:
        return {
            "mode": "warehouse",
            "session": {},
            "rows": [],
            "errors": [
                "Live provider access is disabled in Analytics. "
                "Run the Data Engine to publish a validated snapshot, then use warehouse analytics."
            ],
        }

    tickers = tickers or ["AAPL", "MSFT", "JPM"]
    universe = load_poc_universe()
    selected = {ticker.upper() for ticker in tickers}
    companies = tuple(company for company in universe if company.ticker in selected)
    if not companies:
        return {
            "mode": "demo",
            "session": {},
            "rows": [],
            "errors": ["No tickers selected"],
        }

    now = datetime.now(UTC)
    week = completed_friday(now)
    bars, facts = demo_inputs(companies, week)
    session_date = week
    ny = ZoneInfo("America/New_York")
    open_at = datetime.combine(session_date, datetime.min.time(), ny).replace(hour=9, minute=30)
    close_at = datetime.combine(session_date, datetime.min.time(), ny).replace(hour=16)

    session = DemoMarketSession(
        True,
        session_date,
        session_date - timedelta(days=1),
        open_at,
        close_at,
        "Séance fictive ouverte",
    )
    now = datetime.combine(session_date, datetime.min.time(), ny).replace(hour=14, minute=30)
    quotes = {company.ticker: DemoQuote(demo_price, now) for company in companies}
    intraday = {}
    previous = {company.ticker: 100.0 for company in companies}
    rows = []
    for company in companies:
        row = assess_live(
            company,
            bars.get(company.ticker, {}),
            bars.get("SPY", {}),
            facts.get(company.cik, {}),
            week,
            quotes.get(company.ticker),
            session,
            now,
            close_quote=None,
        )
        price = row.get("price")
        row["day_change"] = price / previous[company.ticker] - 1 if price else None
        rows.append(row)

    return {
        "mode": "demo",
        "session": {
            "is_open": session.is_open,
            "trading_date": session.trading_date.isoformat(),
            "last_close_date": session.last_close_date.isoformat(),
            "label": session.label,
        },
        "refreshed_at": datetime.now(UTC).isoformat(),
        "intraday": intraday,
        "rows": rows,
    }


@dataclass(frozen=True)
class DemoQuote:
    price: float
    timestamp: datetime


@dataclass(frozen=True)
class DemoMarketSession:
    is_open: bool
    trading_date: date
    last_close_date: date
    open_at: datetime
    close_at: datetime
    label: str
