"""Entirely fictional, deterministic data for an offline threshold walkthrough."""

from __future__ import annotations

from datetime import date, timedelta
from math import exp, sin

from finlab.ingest.universe import Company
from finlab.research.weekly import Bar, Bars, Filing


def demo_inputs(companies: tuple[Company, ...], week: date) -> tuple[Bars, dict]:
    """Produce five fictional years with USD 4 EPS and a USD 100 yield ceiling.

    Real company labels are used solely as watchlist controls. These prices,
    revenue, earnings and filing identifiers are all invented. The last stock
    price is USD 99 so a move to USD 101 changes the valuation criterion.
    """
    start = week - timedelta(weeks=261, days=7)
    symbols = tuple(dict.fromkeys(("SPY", *(c.ticker for c in companies))))
    bars: Bars = {symbol: {} for symbol in symbols}
    total = (week - start).days
    for i in range(total + 1):
        day = start + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        for symbol in symbols:
            rate = 0.055 if symbol == "SPY" else 0.12
            target = 550.0 if symbol == "SPY" else 99.0
            close = target * exp(rate * (i - total) / 365 + 0.012 * sin((i - total) / 55))
            bars[symbol][day] = Bar(close, close, close)
    year = week.year - 1
    while date(year + 1, 3, 1) > week:
        year -= 1
    period = date(year, 12, 31)
    prior = date(year - 1, 12, 31)
    filed = date(year + 1, 3, 1)
    facts = {
        c.cik: {
            "revenue": [
                Filing(period, 120e9, filed, "fictional-current"),
                Filing(prior, 100e9, date(year, 3, 1), "fictional-prior"),
            ],
            "diluted_eps": [Filing(period, 4.0, filed, "fictional-current")],
        }
        for c in companies
    }
    return bars, facts
