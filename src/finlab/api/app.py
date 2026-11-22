"""FastAPI read-only backend for the live market dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from finlab.analytics.snapshot import SnapshotUnavailable
from finlab.api.research import router as research_router
from finlab.ingest.universe import load_poc_universe
from finlab.research.live import assess_live
from finlab.research.live_demo import demo_inputs
from finlab.research.weekly import completed_friday

app = FastAPI(title="Finlab API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(research_router)


@app.exception_handler(SnapshotUnavailable)
async def snapshot_unavailable(request: Request, exc: SnapshotUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"status": "unavailable", "detail": str(exc)})


@app.get("/api/warehouse/status")
def warehouse_status() -> dict:
    """Real ingested warehouse counts and date coverage."""
    from finlab.analytics.snapshot import attached_views as attach_warehouse_views
    from finlab.analytics.snapshot import published_read_conn as read_conn
    from finlab.config import get_settings

    cfg = get_settings()
    with read_conn(cfg) as con:
        attached = set(attach_warehouse_views(con, cfg))

        def count(name: str) -> int:
            return (
                int(con.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
                if name in attached
                else 0
            )

        publication = con.execute(
            "SELECT epoch,published_at FROM _warehouse_publication"
        ).fetchone()
        return {
            "warehouse_epoch": publication[0],
            "published_at": publication[1],
            "price_label": "Latest available daily closes; delayed observations, not guaranteed real time",
            "datasets": {
                "entity": count("entity"),
                "filing": count("filing"),
                "security": count("security"),
                "fact": count("fact"),
                "price_daily": count("price_daily"),
            },
            "fundamental_range": (
                list(con.execute("SELECT min(filed_date), max(filed_date) FROM fact").fetchone())
                if "fact" in attached
                else [None, None]
            ),
            "price_range": (
                list(con.execute("SELECT min(date), max(date) FROM price_daily").fetchone())
                if "price_daily" in attached
                else [None, None]
            ),
        }


@app.get("/api/warehouse/assets/{ticker}")
def warehouse_asset(ticker: str, as_of: date | None = None) -> dict:
    """Real company identity, latest filing, latest facts and recent prices."""
    from finlab.analytics.snapshot import attached_views as attach_warehouse_views
    from finlab.analytics.snapshot import published_read_conn as read_conn
    from finlab.config import get_settings

    cutoff = as_of or date.today()
    ticker = ticker.upper()
    cfg = get_settings()
    with read_conn(cfg) as con:
        attached = set(attach_warehouse_views(con, cfg))
        if "security" not in attached or "entity" not in attached:
            return {
                "ticker": ticker,
                "available": False,
                "reason": "Warehouse identity data unavailable",
            }
        identity = con.execute(
            "SELECT e.cik, e.name, e.sector, s.security_id, s.currency, s.exchange "
            "FROM entity e JOIN security s ON s.cik = e.cik WHERE s.ticker = ? LIMIT 1",
            [ticker],
        ).fetchone()
        if identity is None:
            return {"ticker": ticker, "available": False, "reason": "Ticker absent from warehouse"}
        cik, name, sector, security_id, currency, exchange = identity

        latest_filing = con.execute(
            "SELECT accession_number, form, filed_date, period_of_report, fiscal_year, fiscal_period, primary_doc_url "
            "FROM filing WHERE cik = ? AND filed_date <= ? ORDER BY filed_date DESC, accession_number DESC LIMIT 1",
            [cik, cutoff],
        ).fetchone()
        facts = []
        if "fact" in attached:
            from finlab.core.pit import install_macros

            install_macros(con)
            facts = con.execute(
                "SELECT 'canonical',canonical,CASE WHEN canonical='eps_diluted' THEN 'USD/shares' ELSE 'USD' END,period_end,duration_bucket,value,filed_date,accession_number FROM fundamentals_asof(?) WHERE cik=? ORDER BY period_end DESC,filed_date DESC,canonical LIMIT 40",
                [cutoff, cik],
            ).fetchall()
        prices = con.execute(
            "SELECT date, open, high, low, close, volume, source "
            "FROM price_daily WHERE security_id = ? AND date <= ? ORDER BY date DESC LIMIT 250",
            [security_id, cutoff],
        ).fetchall()
        return {
            "ticker": ticker,
            "as_of": str(cutoff),
            "available": True,
            "company": {
                "cik": cik,
                "name": name,
                "sector": sector,
                "currency": currency,
                "exchange": exchange,
                "security_id": security_id,
            },
            "latest_filing": None
            if latest_filing is None
            else {
                "accession_number": latest_filing[0],
                "form": latest_filing[1],
                "filed_date": str(latest_filing[2]),
                "period_of_report": str(latest_filing[3]) if latest_filing[3] else None,
                "fiscal_year": latest_filing[4],
                "fiscal_period": latest_filing[5],
                "primary_doc_url": latest_filing[6],
            },
            "facts": [
                {
                    "taxonomy": r[0],
                    "tag": r[1],
                    "unit": r[2],
                    "period_end": str(r[3]),
                    "duration_bucket": r[4],
                    "value": r[5],
                    "filed_date": str(r[6]),
                    "accession_number": r[7],
                }
                for r in facts
            ],
            "prices": [
                {
                    "date": str(r[0]),
                    "open": r[1],
                    "high": r[2],
                    "low": r[3],
                    "close": r[4],
                    "volume": r[5],
                    "source": r[6],
                }
                for r in reversed(prices)
            ],
        }


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


@app.get("/api/data/status")
def data_status() -> dict:
    """Read-only ingestion status published by the Data Engine."""
    from finlab.config import get_settings
    from finlab.engine.status import read_status

    return read_status(get_settings().data_dir / "control")


@app.post("/api/data/refresh")
def data_refresh() -> dict:
    """Request engine work without touching providers or the financial writer."""
    from finlab.config import get_settings
    from finlab.engine.control import request_refresh

    request_id = request_refresh(get_settings().data_dir)
    return {
        "accepted": True,
        "request_id": request_id,
        "message": "Queued for the local Data Engine; it must be running.",
    }


@app.get("/api/analytics/rankings")
def analytics_rankings(as_of: Annotated[date | None, Query()] = None) -> dict:
    from finlab.analytics.rankings import historical_rankings
    from finlab.analytics.status import latest_research_date

    cutoff = as_of or latest_research_date()
    result = historical_rankings(cutoff)
    result["freshness"] = {
        "status": "historical"
        if as_of
        else "stale"
        if (date.today() - cutoff).days > 7
        else "latest_available_daily",
        "observed_as_of": str(cutoff),
        "quote_guarantee": "Delayed daily warehouse observations; no real-time guarantee",
    }
    return result


@app.get("/api/analytics/recommendations")
def historical_recommendations(as_of: date | None = None) -> dict:
    from finlab.analytics.recommendations import recommendation_report

    return recommendation_report(as_of or date.today())


@app.get("/api/warehouse/quality")
def warehouse_quality(as_of: date | None = None) -> dict:
    from finlab.analytics.quality import warehouse_quality as inspect_quality

    return inspect_quality(as_of or date.today())


@app.get("/api/analytics/factors/{identity}")
def company_factor_details(identity: str, as_of: date | None = None) -> dict:
    from fastapi import HTTPException

    from finlab.analytics.company import company_factors

    try:
        return company_factors(identity, as_of or date.today())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
