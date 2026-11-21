"""FastAPI read-only backend for the live market dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
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


@app.get("/api/warehouse/status")
def warehouse_status() -> dict:
    """Real ingested warehouse counts and date coverage."""
    from finlab.config import get_settings
    from finlab.db.conn import attach_warehouse_views, read_conn

    cfg = get_settings()
    with read_conn(cfg) as con:
        attached = set(attach_warehouse_views(con, cfg))

        def count(name: str) -> int:
            return (
                int(con.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
                if name in attached
                else 0
            )

        return {
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
def warehouse_asset(ticker: str) -> dict:
    """Real company identity, latest filing, latest facts and recent prices."""
    from finlab.config import get_settings
    from finlab.db.conn import attach_warehouse_views, read_conn

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
            "FROM filing WHERE cik = ? ORDER BY filed_date DESC, accession_number DESC LIMIT 1",
            [cik],
        ).fetchone()
        facts = con.execute(
            "SELECT taxonomy, tag, unit, period_end, duration_bucket, value, filed_date, accession_number "
            "FROM fact WHERE cik = ? ORDER BY filed_date DESC, period_end DESC LIMIT 20",
            [cik],
        ).fetchall()
        prices = con.execute(
            "SELECT date, open, high, low, close, volume, source "
            "FROM price_daily WHERE security_id = ? ORDER BY date DESC LIMIT 250",
            [security_id],
        ).fetchall()
        return {
            "ticker": ticker,
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
    from finlab.engine.status import read_status

    return read_status(Path(__file__).resolve().parents[3] / ".finlab" / "ingestion")


@app.post("/api/data/refresh")
def data_refresh() -> dict:
    """Refresh requests are enqueued by the Data Engine CLI, never written here."""
    return {
        "accepted": False,
        "message": "Use the Data Engine command: make ingest or make prices-yahoo.",
    }


@app.get("/api/analytics/rankings")
def analytics_rankings(as_of: Annotated[str | None, Query()] = None) -> dict:
    """Rank local securities using real completed-price history and SEC facts."""
    from datetime import date
    from math import isfinite, sqrt

    from finlab.config import get_settings
    from finlab.db.conn import attach_warehouse_views, read_conn

    cutoff = date.fromisoformat(as_of) if as_of else date.today()
    cfg = get_settings()
    rows: list[dict] = []
    with read_conn(cfg) as con:
        attached = set(attach_warehouse_views(con, cfg))
        if "security" not in attached or "entity" not in attached or "price_daily" not in attached:
            return {
                "as_of": cutoff.isoformat(),
                "rows": [],
                "reason": "Warehouse price/security data unavailable",
            }

        securities = con.execute(
            "SELECT s.security_id, s.ticker, e.name, e.sector, s.currency "
            "FROM security s JOIN entity e ON e.cik = s.cik "
            "WHERE s.is_active = TRUE AND s.ticker IS NOT NULL ORDER BY s.ticker"
        ).fetchall()

        for security_id, ticker, name, sector, currency in securities:
            prices = con.execute(
                "SELECT date, close FROM price_daily "
                "WHERE security_id = ? AND date <= ? AND close IS NOT NULL "
                "ORDER BY date DESC LIMIT 252",
                [security_id, cutoff],
            ).fetchall()
            if len(prices) < 21:
                continue
            ordered = list(reversed(prices))
            closes = [float(r[1]) for r in ordered]
            latest = closes[-1]

            def period_return(
                days: int, values: list[float] = closes, last: float = latest
            ) -> float | None:
                if len(values) <= days:
                    return None
                prior = values[-days - 1]
                return last / prior - 1 if prior > 0 else None

            returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
            finite_returns = [r for r in returns if isfinite(r)]
            volatility = (
                (sum(r * r for r in finite_returns) / len(finite_returns)) ** 0.5 * sqrt(252)
                if finite_returns
                else None
            )
            peak = closes[0]
            drawdown = 0.0
            for close in closes:
                peak = max(peak, close)
                drawdown = min(drawdown, close / peak - 1)

            latest_eps = None
            latest_revenue = None
            if "fact" in attached:
                cik_row = con.execute(
                    "SELECT cik FROM security WHERE security_id = ? LIMIT 1", [security_id]
                ).fetchone()
                if cik_row:
                    annual_facts = con.execute(
                        "SELECT tag, value, period_end, filed_date FROM fact "
                        "WHERE cik = ? AND filed_date <= ? AND duration_bucket = 'annual' "
                        "AND tag IN ('Revenues','RevenueFromContractWithCustomerExcludingAssessedTax','SalesRevenueNet','NetIncomeLoss','EarningsPerShareDiluted') "
                        "ORDER BY period_end DESC, filed_date DESC LIMIT 12",
                        [cik_row[0], cutoff],
                    ).fetchall()
                    latest_revenue = next(
                        (
                            float(r[1])
                            for r in annual_facts
                            if r[0]
                            in (
                                "Revenues",
                                "RevenueFromContractWithCustomerExcludingAssessedTax",
                                "SalesRevenueNet",
                            )
                        ),
                        None,
                    )
                    latest_eps = next(
                        (float(r[1]) for r in annual_facts if r[0] == "EarningsPerShareDiluted"),
                        None,
                    )

            earnings_yield = latest_eps / latest if latest_eps is not None and latest > 0 else None
            momentum = period_return(252)
            relative_momentum = (period_return(126) or 0) - (period_return(21) or 0)
            components = [
                v
                for v in (
                    earnings_yield,
                    momentum,
                    relative_momentum,
                    -volatility if volatility is not None else None,
                )
                if v is not None and isfinite(v)
            ]
            score = 50.0
            if components:
                positive = sum(1 for v in components if v >= 0)
                score = max(
                    0.0, min(100.0, 50.0 + 10.0 * (positive - (len(components) - positive)))
                )

            category = "Insufficient data"
            if earnings_yield is not None or momentum is not None:
                category = (
                    "High-priority research"
                    if score >= 65
                    else "Attractive research"
                    if score >= 55
                    else "Neutral"
                    if score >= 45
                    else "Caution"
                )

            rows.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "sector": sector,
                    "currency": currency or "USD",
                    "price": latest,
                    "earnings_yield": earnings_yield,
                    "momentum_12m": momentum,
                    "momentum_3m": period_return(63),
                    "momentum_1m": period_return(21),
                    "volatility": volatility,
                    "max_drawdown": drawdown,
                    "revenue": latest_revenue,
                    "score": round(score, 1),
                    "category": category,
                    "data_status": "complete"
                    if len(prices) >= 252 and earnings_yield is not None
                    else "partial",
                }
            )

    rows.sort(key=lambda r: (-r["score"], r["ticker"]))
    return {
        "as_of": cutoff.isoformat(),
        "method": "earnings_yield/momentum/risk descriptive research",
        "rows": rows,
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
