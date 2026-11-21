"""Fictional prices and financial statements for the dashboard's offline walkthrough."""

from datetime import date, timedelta
from math import exp, sin

from finlab.ingest.universe import Company
from finlab.research.weekly import MODEL, Bar, Filing, screen


def example_brief() -> dict:
    week = date(2025, 7, 11)
    start = week - timedelta(weeks=261)
    companies = [
        Company(1, "EXAMPLE-A", "Industrials", "Example A"),
        Company(2, "EXAMPLE-B", "Technology", "Example B"),
    ]
    bars = {symbol: {} for symbol in ("SPY", "EXAMPLE-A", "EXAMPLE-B")}
    for i in range((week - start).days + 1):
        day = start + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        for symbol, rate, phase in (
            ("SPY", 0.06, 0),
            ("EXAMPLE-A", 0.11, 1),
            ("EXAMPLE-B", 0.035, 2),
        ):
            close = 50 * exp(rate * i / 365 + 0.045 * sin(i / 63 + phase))
            bars[symbol][day] = Bar(close, close, close)
    facts = {
        c.cik: {
            "revenue": [
                Filing(date(2024, 12, 31), 120e6, date(2025, 2, 14), "fictional"),
                Filing(date(2023, 12, 31), 100e6, date(2024, 2, 16), "fictional"),
            ],
            "diluted_eps": [
                Filing(date(2024, 12, 31), 5 if c.cik == 1 else 1, date(2025, 2, 14), "fictional")
            ],
        }
        for c in companies
    }
    return {
        "week": week.isoformat(),
        "source": "Simulated prices and filings",
        "generated_at": "2025-07-12",
        "model": MODEL,
        "benchmark": "Simulated SPY",
        "rows": screen(companies, bars, facts, week),
    }
