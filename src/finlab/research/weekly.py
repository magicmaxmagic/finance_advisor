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


def reprice(
    row: dict,
    price: float | None,
    *,
    price_date: str,
    blocked_reason: str | None = None,
) -> dict:
    """Apply the same four gates to either a weekly close or a live entry price.

    Component errors travel with the computed inputs. Repricing never makes a
    missing, stale or incompatible component valid. ``None`` means unknown,
    distinct from a measured criterion that failed.
    """
    result = dict(row)
    errors = dict(row.get("component_errors", {}))
    valid_price = price is not None and isfinite(price) and price > 0
    if blocked_reason:
        errors["earnings_yield"] = blocked_reason
    elif not valid_price:
        errors["earnings_yield"] = "Missing a positive, finite entry price."
    eps = row.get("eps")
    if eps is None:
        eps = row.get("evidence", {}).get("Diluted EPS", {}).get("value")
    earnings_yield = eps / price if valid_price and eps is not None and isfinite(eps) else None
    values = {
        "earnings_yield": earnings_yield,
        "growth": row.get("growth"),
        "excess": row.get("excess"),
        "drawdown": row.get("drawdown"),
    }
    definitions = [
        ("earnings_yield", "Annual diluted earnings yield", ">= 4%", MIN_YIELD, "ge"),
        ("growth", "Year-over-year annual revenue growth", "> 0%", 0.0, "gt"),
        ("excess", "52-to-4-week return spread over SPY", "> 0 pp", 0.0, "gt"),
        ("drawdown", "Worst decline between weekly closes", "<= 35% decline", -MAX_DRAWDOWN, "ge"),
    ]
    references = dict(row.get("references", {}))
    references["price"] = price_date
    tests = []
    for key, name, threshold, bound, operator in definitions:
        value = values[key]
        error = errors.get(key)
        if value is None or not isfinite(value):
            error = error or f"Missing or invalid {name.lower()}."
        passed = None if error else (value >= bound if operator == "ge" else value > bound)
        if error:
            detail = error
        elif key == "excess":
            detail = f"{name} {value * 100:+.1f} pp (must be positive)."
        elif key == "growth":
            detail = f"{name} {value:+.1%} (must be positive)."
        elif key == "drawdown":
            detail = f"{name} {value:.1%} (limit 35%)."
        else:
            detail = f"{name} {value:.1%} (minimum 4%)."
        reference = references.get(key, "Unavailable")
        if key == "earnings_yield":
            reference = f"EPS: {references.get('eps', 'Unavailable')}; price: {price_date}"
        tests.append(
            {
                "key": key,
                "name": name,
                "value": value,
                "threshold": threshold,
                "passed": passed,
                "detail": detail,
                "reference": reference,
            }
        )
    unknown = [test["detail"] for test in tests if test["passed"] is None]
    failed = [test["detail"] for test in tests if test["passed"] is False]
    reason = " ".join(dict.fromkeys(unknown or failed)) or "All four published criteria passed."
    status = "Insufficient data" if unknown else "Watch" if failed else "Buy candidate"
    ceiling = max(0.0, eps / MIN_YIELD) if eps is not None and isfinite(eps) else None
    # Scenarios must not silently reuse an EPS whose share basis or fiscal vintage is invalid.
    if row.get("component_errors", {}).get("earnings_yield"):
        ceiling = None
    result.update(
        status=status,
        reason=reason,
        tests=tests,
        price=price,
        quote_date=price_date,
        earnings_yield=earnings_yield,
        references=references,
        yield_price_ceiling=ceiling,
        ceiling_gap=(ceiling / price - 1) if ceiling is not None and valid_price else None,
        stressed_yield=(
            earnings_yield * 0.8
            if earnings_yield is not None and eps > 0 and ceiling is not None
            else None
        ),
        stressed_price_ceiling=ceiling * 0.8 if ceiling is not None else None,
    )
    return result


def assess(
    ticker: str,
    cik: int,
    sector: str,
    series: dict[date, Bar],
    market: dict[date, Bar],
    sessions: dict[date, date],
    week: date,
    facts: dict[str, list[Filing]],
    *,
    facts_as_of: date | None = None,
    basis_as_of: date | None = None,
) -> dict:
    """Compute completed-period components, then apply the shared decision gates.

    Live callers can resolve newer fiscal releases while keeping momentum and
    drawdown fixed to a completed week. Price history beyond that week is used
    only to check the EPS/share basis, never for the historical signal.
    """
    cutoff = facts_as_of or week
    basis_cutoff = basis_as_of or week
    row = {"ticker": ticker, "sector": sector, "cik": cik, "week": week.isoformat()}
    errors: dict[str, str] = {}
    references: dict[str, str] = {}
    path = _matched_path(series, market, sessions, week, 52)
    price = None
    price_date = week.isoformat()
    if not path:
        message = "Missing one or more of 53 matching weekly stock and SPY closes."
        errors.update(excess=message, drawdown=message)
    else:
        session = path[-1][0]
        price = series[session].raw
        price_date = session.isoformat()
        stock = np.array([p[1] for p in path])
        index = np.array([p[2] for p in path])
        momentum = float(stock[-5] / stock[0] - 1)
        benchmark_momentum = float(index[-5] / index[0] - 1)
        drawdown = float(np.min(stock / np.maximum.accumulate(stock) - 1))
        row.update(
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
        references["excess"] = f"{path[0][0]} → {path[-5][0]} (completed sessions)"
        references["drawdown"] = f"{path[0][0]} → {session} (completed weekly closes)"
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
        "win_frequency": sum(x > 0 for x in outcomes) / len(outcomes) if outcomes else None,
        "median_excess": float(np.median(outcomes)) if outcomes else None,
        "worst_excess": min(outcomes) if outcomes else None,
    }
    # The macro applies PIT. This independent guard also protects non-SQL callers.
    known = {
        key: sorted(
            (f for f in values if f.filed <= cutoff and f.period <= f.filed),
            key=lambda f: (f.period, f.filed, f.accession),
            reverse=True,
        )
        for key, values in facts.items()
    }
    revenue, earnings = known.get("revenue", []), known.get("diluted_eps", [])
    current = revenue[0] if revenue else None
    eps = earnings[0] if earnings else None
    prior = next(
        (f for f in revenue if current and 350 <= (current.period - f.period).days <= 380), None
    )
    evidence = {}
    for name, filing in (
        ("Diluted EPS", eps),
        ("Annual revenue", current),
        ("Prior annual revenue", prior),
    ):
        if filing:
            evidence[name] = filing.evidence(cik)
    row["evidence"] = evidence
    if eps:
        references["eps"] = f"FY {eps.period}; filed {eps.filed}"
    if current:
        references["growth"] = (
            f"FY {prior.period if prior else '?'} → {current.period}; filed {current.filed}"
        )
    fundamental_error = None
    if not current or not eps:
        fundamental_error = "Missing reported annual USD revenue or diluted EPS."
    elif (
        prior is None
        or eps.period != current.period
        or (cutoff - current.period).days > 460
        or prior.value <= 0
        or current.value < 0
        or not all(isfinite(f.value) for f in (eps, current, prior))
    ):
        fundamental_error = "Annual figures are stale, incomplete or cover different fiscal years."
    if fundamental_error:
        errors.update(earnings_yield=fundamental_error, growth=fundamental_error)
    else:
        row.update(eps=eps.value, growth=current.value / prior.value - 1)
        # EPS can be restated after a split. Without reliable split-vintage metadata,
        # abstain rather than assuming a conversion, including offsetting splits.
        basis_dates = sorted(d for d in series if current.period - 7 * DAY <= d <= basis_cutoff)
        basis = series[basis_dates[-1]].split / series[basis_dates[-1]].raw if basis_dates else None
        if (
            not basis_dates
            or basis_dates[0] > current.period
            or (basis_cutoff - basis_dates[-1]).days > 7
            or any((b - a).days > 7 for a, b in pairwise(basis_dates))
            or any(
                not isclose(series[d].split / series[d].raw, basis, rel_tol=0.002)
                for d in basis_dates
            )
        ):
            errors["earnings_yield"] = (
                "A split or a price gap prevents a reliable EPS-to-price comparison."
            )
    row.update(component_errors=errors, references=references)
    return reprice(row, price, price_date=price_date)


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
            -(r.get("earnings_yield") or 0),
            -(r.get("excess") or 0),
            r["ticker"],
        ),
    )
