"""A rules-based US equity screen. All dates refer to completed trading sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from math import isclose, isfinite
from zoneinfo import ZoneInfo

import numpy as np
from dateutil.easter import easter

DAY = timedelta(days=1)
WEEK = timedelta(weeks=1)
BENCHMARK = "SPY"
MIN_YIELD = 0.04
MAX_DRAWDOWN = 0.35
MODEL = "weekly-us-equity-v1"


@dataclass(frozen=True)
class Bar:
    raw: float
    split: float
    adjusted: float

    def __post_init__(self) -> None:
        if not all(isfinite(p) and p > 0 for p in (self.raw, self.split, self.adjusted)):
            raise ValueError("Prices must be positive and finite.")


Bars = dict[str, dict[date, Bar]]


@dataclass(frozen=True)
class Filing:
    period: date
    value: float
    filed: date
    accession: str

    def evidence(self, cik: int) -> dict:
        folder = self.accession.replace("-", "")
        return {
            "period": self.period.isoformat(),
            "value": self.value,
            "filed": self.filed.isoformat(),
            "accession": self.accession,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{self.accession}-index.htm",
        }


def completed_friday(now: datetime) -> date:
    if now.tzinfo is None:
        raise ValueError("Pass a timezone-aware timestamp.")
    local = now.astimezone(ZoneInfo("America/New_York"))
    friday = local.date() - timedelta(days=(local.weekday() - 4) % 7)
    # Leave time for late bars and Friday evening EDGAR releases.
    return friday - WEEK if local.date() == friday else friday


def weekly_sessions(series: dict[date, Bar], cutoff: date) -> dict[date, date]:
    """Map each calendar Friday to the benchmark's last actual session of that week."""
    fridays = {d + (4 - d.weekday()) * DAY for d in series if d <= cutoff and d.weekday() < 5}
    sessions = {}
    for friday in fridays:
        # A missing ordinary Friday is stale data. Accept Thursday only
        # on scheduled Friday closures; an extraordinary closure fails closed.
        fixed = {date(friday.year, 7, 4), date(friday.year, 12, 25)}
        if friday.year >= 2022:
            fixed.add(date(friday.year, 6, 19))
        closed = (
            friday == easter(friday.year) - 2 * DAY
            or (friday.month, friday.day) == (1, 1)
            or friday in fixed
            or friday + DAY in fixed
        )
        expected = friday - DAY if closed else friday
        if expected in series:
            sessions[friday] = expected
    return sessions


def _matched_path(series: dict, market: dict, sessions: dict, end: date, weeks: int) -> list:
    path = []
    for i in range(weeks, -1, -1):
        session = sessions.get(end - i * WEEK)
        if session is None or session not in series:
            return []
        path.append((session, series[session].adjusted, market[session].adjusted))
    return path


def assess(
    ticker: str,
    cik: int,
    sector: str,
    series: dict[date, Bar],
    market: dict[date, Bar],
    sessions: dict[date, date],
    week: date,
    facts: dict[str, list[Filing]],
) -> dict:
    """Assess one issuer using only fiscal observations and prices available by Friday."""
    row = {"ticker": ticker, "sector": sector, "cik": cik, "status": "Insufficient data"}
    path = _matched_path(series, market, sessions, week, 52)
    if not path:
        return row | {"reason": "Missing one or more of 53 matching weekly stock and SPY closes."}
    session = path[-1][0]
    price = series[session].raw
    stock = np.array([p[1] for p in path])
    index = np.array([p[2] for p in path])
    momentum = float(stock[-5] / stock[0] - 1)
    benchmark_momentum = float(index[-5] / index[0] - 1)
    drawdown = float(np.min(stock / np.maximum.accumulate(stock) - 1))
    row.update(
        quote_date=session.isoformat(),
        price=price,
        momentum=momentum,
        benchmark_momentum=benchmark_momentum,
        excess=momentum - benchmark_momentum,
        drawdown=drawdown,
        chart=[
            {
                "date": day.isoformat(),
                "Stock": round(100 * a / stock[0], 3),
                "SPY": round(100 * b / index[0], 3),
            }
            for day, a, b in path
        ],
    )
    # Disjoint intervals; every endpoint was observed by the report date.
    outcomes = []
    for i in range(20):
        end = week - i * 13 * WEEK
        block = _matched_path(series, market, sessions, end, 13)
        if block:
            _, a0, b0 = block[0]
            _, a1, b1 = block[-1]
            outcomes.append(a1 / a0 - b1 / b0)
    row["history"] = {
        "windows": len(outcomes),
        "wins": sum(x > 0 for x in outcomes),
        "median_excess": float(np.median(outcomes)),
        "worst_excess": min(outcomes),
    }
    # The macro applies the PIT cutoff. The independent guard also protects other callers.
    known = {
        key: sorted(
            (f for f in values if f.filed <= week and f.period <= f.filed),
            key=lambda f: f.period,
            reverse=True,
        )
        for key, values in facts.items()
    }
    revenue, earnings = known.get("revenue", []), known.get("diluted_eps", [])
    if not revenue or not earnings:
        return row | {"reason": "Missing reported annual USD revenue or diluted EPS."}
    current = revenue[0]
    eps = earnings[0]
    prior = next((f for f in revenue if 350 <= (current.period - f.period).days <= 380), None)
    if (
        prior is None
        or eps.period != current.period
        or (week - current.period).days > 460
        or prior.value <= 0
        or current.value < 0
        or not all(isfinite(f.value) for f in (eps, current, prior))
    ):
        return row | {
            "reason": "Annual figures are stale, incomplete or cover different fiscal years."
        }
    # EPS may be retroactively restated for splits. Abstain whenever the raw/share
    # basis changed between fiscal year-end and today, including offsetting splits.
    basis = series[session].split / price
    first = current.period - 7 * DAY
    basis_dates = sorted(d for d in series if first <= d <= session)
    if (
        not basis_dates
        or basis_dates[0] > current.period
        or any((b - a).days > 7 for a, b in pairwise(basis_dates))
        or any(
            not isclose(series[d].split / series[d].raw, basis, rel_tol=0.002) for d in basis_dates
        )
    ):
        return row | {
            "reason": "A split or a price gap prevents a reliable EPS-to-price comparison."
        }
    growth = current.value / prior.value - 1
    earnings_yield = eps.value / price
    gates = [
        (
            earnings_yield >= MIN_YIELD,
            f"Annual diluted earnings yield {earnings_yield:.1%} (minimum 4%).",
        ),
        (growth > 0, f"Year-over-year annual revenue growth {growth:+.1%} (must be positive)."),
        (
            row["excess"] > 0,
            f"52-to-4-week return spread over SPY {row['excess'] * 100:+.1f} pp (must be positive).",
        ),
        (
            drawdown >= -MAX_DRAWDOWN,
            f"Worst decline between weekly closes {drawdown:.1%} (limit 35%).",
        ),
    ]
    failed = [message for passed, message in gates if not passed]
    return row | {
        "status": "Watch" if failed else "Buy candidate",
        "reason": " ".join(failed) if failed else "All four published criteria passed.",
        "tests": [{"passed": passed, "detail": detail} for passed, detail in gates],
        "earnings_yield": earnings_yield,
        "growth": growth,
        "yield_price_ceiling": max(0.0, eps.value / MIN_YIELD),
        "stressed_yield": earnings_yield * 0.8 if eps.value > 0 else None,
        "evidence": {
            "Diluted EPS": eps.evidence(cik),
            "Annual revenue": current.evidence(cik),
            "Prior annual revenue": prior.evidence(cik),
        },
    }


def screen(companies: list, bars: Bars, facts: dict, week: date) -> list[dict]:
    if week.weekday() != 4:
        raise ValueError("A weekly cutoff must be a Friday.")
    market = bars.get(BENCHMARK, {})
    sessions = weekly_sessions(market, week)
    if week not in sessions:
        raise ValueError(f"Missing a fresh SPY benchmark close for the week ending {week}.")
    rows = [
        assess(
            c.ticker,
            c.cik,
            c.sector,
            bars.get(c.ticker, {}),
            market,
            sessions,
            week,
            facts.get(c.cik, {}),
        )
        for c in companies
    ]
    order = {"Buy candidate": 0, "Watch": 1, "Insufficient data": 2}
    return sorted(
        rows,
        key=lambda r: (
            order[r["status"]],
            -r.get("earnings_yield", 0),
            -r.get("excess", 0),
            r["ticker"],
        ),
    )
