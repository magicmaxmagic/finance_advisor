"""FastAPI read-only backend for the live market dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from finlab.config import get_settings
from finlab.ingest.live_facts import FundamentalsError, load_facts
from finlab.ingest.live_market import MarketDataError, MarketSession, Quote, fetch_snapshot
from finlab.ingest.universe import load_poc_universe
from finlab.research.live import assess_live
from finlab.research.live_demo import demo_inputs
from finlab.research.weekly import completed_friday
from finlab.ui.live_data import cached_history

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
    """Compute a read-only live/demonstration screen for selected tickers."""
    tickers = tickers or ["AAPL", "MSFT", "JPM"]
    settings = get_settings()
    universe = load_poc_universe()
    selected = {ticker.upper() for ticker in tickers}
    companies = tuple(company for company in universe if company.ticker in selected)
    if not companies:
        return {
            "mode": "demo" if demo else "live",
            "session": {},
            "rows": [],
            "errors": ["No tickers selected"],
        }

    now = datetime.now(UTC)
    week = completed_friday(now)
    symbols = tuple(sorted({"SPY", *(company.ticker for company in companies)}))

    if demo:
        bars, facts = demo_inputs(companies, week)
        session_date = week
        ny = ZoneInfo("America/New_York")
        open_at = datetime.combine(session_date, datetime.min.time(), ny).replace(hour=9, minute=30)
        close_at = datetime.combine(session_date, datetime.min.time(), ny).replace(hour=16)
        session = MarketSession(
            True,
            session_date,
            session_date - timedelta(days=1),
            open_at,
            close_at,
            "Séance fictive ouverte",
        )
        now = datetime.combine(session_date, datetime.min.time(), ny).replace(hour=14, minute=30)
        quotes = {company.ticker: Quote(demo_price, now) for company in companies}
        closes = {}
        intraday = {}
        previous = {company.ticker: 100.0 for company in companies}
        refreshed = datetime.now(UTC)
        history_error = None
    else:
        try:
            snapshot = fetch_snapshot(settings, symbols, now)
        except MarketDataError as exc:
            return {"mode": "live", "session": {}, "rows": [], "errors": [str(exc)]}
        session = snapshot.session
        quotes = snapshot.quotes
        closes = snapshot.closes
        previous = snapshot.previous_closes
        intraday = snapshot.intraday
        refreshed = snapshot.refreshed_at
        try:
            bars = cached_history(settings, symbols, session.last_close_date)
        except (MarketDataError, ValueError, OSError) as exc:
            bars = {}
            history_error = f"Historique Alpaca indisponible : {exc}"
        else:
            history_error = None
        try:
            ny = ZoneInfo("America/New_York")
            facts = load_facts(settings, companies, now.astimezone(ny).date())[0]
        except (FundamentalsError, ValueError, OSError):
            facts = {}
            history_error = (
                "SEC : données fondamentales indisponibles"
                if history_error is None
                else history_error
            )

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
            close_quote=closes.get(company.ticker),
            historical_error=history_error,
        )
        row["week"] = week.isoformat()
        quote = quotes.get(company.ticker) if session.is_open else closes.get(company.ticker)
        row["transaction_time"] = quote.timestamp.isoformat(timespec="seconds") if quote else None
        price = row.get("price")
        row["day_change"] = (
            price / previous.get(company.ticker) - 1
            if price and previous.get(company.ticker)
            else None
        )
        rows.append(row)

    return {
        "mode": "demo" if demo else "live",
        "session": {
            "is_open": session.is_open,
            "trading_date": session.trading_date.isoformat(),
            "last_close_date": session.last_close_date.isoformat(),
            "label": session.label,
        },
        "refreshed_at": refreshed.isoformat(),
        "intraday": intraday,
        "rows": rows,
    }
