"""Live entry-price decisions over completed-period research components."""

from __future__ import annotations

from datetime import datetime
from math import isclose
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from finlab.research.weekly import Bar, Filing, assess, reprice, weekly_sessions

if TYPE_CHECKING:
    from datetime import date

    from finlab.ingest.live_market import MarketSession, Quote
    from finlab.ingest.universe import Company

NY = ZoneInfo("America/New_York")
MAX_TRADE_AGE_SECONDS = 90


def assess_live(
    company: Company,
    series: dict[date, Bar],
    market: dict[date, Bar],
    facts: dict[str, list[Filing]],
    week: date,
    quote: Quote | None,
    session: MarketSession,
    now: datetime,
    *,
    close_quote: Quote | None = None,
    historical_error: str | None = None,
    share_basis_error: str | None = None,
) -> dict:
    """Reprice the shared screen, suspending signals on unusable market inputs.

    Calendar dates are authoritative; a last trade from a different session
    cannot be substituted for an actual daily close when the market is closed.
    All intraday freshness checks use UTC-aware instants, including DST days.
    """
    if now.tzinfo is None:
        raise ValueError("Pass a timezone-aware current timestamp.")
    as_of = now.astimezone(NY).date()
    row = assess(
        company.ticker,
        company.cik,
        company.sector,
        series,
        market,
        weekly_sessions(market, week),
        week,
        facts,
        facts_as_of=as_of,
        basis_as_of=session.last_close_date,
    )
    reason = historical_error
    price = None
    price_date = "Unavailable"
    age = None
    if session.is_open:
        if quote is None:
            reason = reason or "No latest transaction returned for this symbol (partial response)."
        elif quote.timestamp.tzinfo is None:
            reason = reason or "Latest transaction has no timezone."
        else:
            age = (now - quote.timestamp).total_seconds()
            price, price_date = quote.price, quote.timestamp.isoformat()
            if quote.timestamp.astimezone(NY).date() != session.trading_date:
                reason = reason or "Latest transaction belongs to a different trading session."
            elif age > MAX_TRADE_AGE_SECONDS:
                reason = (
                    reason or f"Latest transaction is stale ({age:.0f} seconds; limit 90 seconds)."
                )
            elif age < -5:
                reason = (
                    reason or "Latest transaction is in the future; provider clock is inconsistent."
                )
    else:
        if close_quote is not None and close_quote.timestamp.tzinfo is not None:
            price, price_date = close_quote.price, close_quote.timestamp.isoformat()
            if close_quote.timestamp.astimezone(NY).date() != session.last_close_date:
                reason = (
                    reason or "Daily close is stale or does not match the last completed session."
                )
        elif session.last_close_date in series:
            price = series[session.last_close_date].raw
            price_date = session.last_close_date.isoformat()
        else:
            reason = reason or f"Missing last completed session close ({session.last_close_date})."
    # Fully split-adjusted history uses today's share basis. A non-unit latest
    # ratio exposes a split since its last raw close, including today's split.
    available = [day for day in series if day <= session.last_close_date]
    if available:
        latest = series[max(available)]
        if not isclose(latest.split / latest.raw, 1.0, rel_tol=0.002):
            row["component_errors"]["earnings_yield"] = (
                "A recent split prevents a reliable EPS-to-current-price comparison."
            )
    if share_basis_error:
        row["component_errors"]["earnings_yield"] = share_basis_error
    result = reprice(row, price, price_date=price_date, blocked_reason=reason)
    result.update(
        quote_age_seconds=age,
        market_open=session.is_open,
        analysis_date=session.trading_date.isoformat()
        if session.is_open
        else session.last_close_date.isoformat(),
        price_kind="Latest transaction" if session.is_open else "Last completed session close",
    )
    return result
