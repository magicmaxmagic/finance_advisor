"""Aligned annual fundamentals; unsupported denominators remain unavailable."""

from datetime import date
from itertools import pairwise
from math import isfinite
from statistics import pstdev

DEFINITIONS = {
    "net_margin": "Annual parent net income / annual revenue, same reporting period.",
    "operating_margin": "Annual operating income / annual revenue, same reporting period.",
    "fcf_margin": "(Annual operating cash flow - selected capital expenditures) / annual revenue.",
    "cash_flow_quality": "Annual operating cash flow / positive annual net income.",
    "roe": "Annual parent net income / average beginning and ending parent equity.",
    "roic": "Operating NOPAT using reported effective tax / average verified invested capital.",
    "earnings_yield": "Annual net income / market value using latest public common shares; share bases aligned.",
    "fcf_yield": "Annual operating cash flow less selected capital expenditures / reported-share market value.",
    "price_to_book": "Reported-share market value / positive latest parent book equity.",
    "price_to_sales": "Reported-share market value / positive annual revenue.",
    "ev_to_operating_ebitda": "(Reported-share market value + verified debt + preferred equity + NCI - cash) / (operating income + combined D&A).",
    "revenue_growth": "Latest annual revenue / prior aligned positive annual revenue - 1.",
    "earnings_growth": "Latest annual net income / prior aligned positive net income - 1.",
    "fcf_growth": "Latest annual free cash flow / prior aligned positive free cash flow - 1.",
    "growth_acceleration": "Current annual revenue growth less prior annual revenue growth.",
    "earnings_variability": "Population standard deviation of five annual net incomes / absolute mean; lower is more stable.",
    "debt_to_assets": "Verified combined interest-bearing debt / same-date positive assets.",
}


def calculate_fundamentals(
    observations: list[dict], as_of: date, market_cap: float | None = None
) -> dict:
    if market_cap is not None and (not isfinite(market_cap) or market_cap <= 0):
        market_cap = None
    eligible = [r for r in observations if r["filed_date"] <= as_of and r["period_end"] <= as_of]
    selected = {}
    for record in sorted(eligible, key=lambda r: (r["filed_date"], r.get("accession_number", ""))):
        selected[(record["canonical"], record["period_end"], record["duration_bucket"])] = record
    annual = sorted(
        {
            r["period_end"]
            for r in selected.values()
            if r["duration_bucket"] == "annual" and r["canonical"] == "revenue"
        },
        reverse=True,
    )
    values = dict.fromkeys(DEFINITIONS)

    def fact(name, day, bucket="annual"):
        row = selected.get((name, day, bucket))
        value = row["value"] if row else None
        return value if value is not None and isfinite(value) else None

    def divide(numerator, denominator):
        return (
            numerator / denominator
            if numerator is not None and denominator is not None and denominator > 0
            else None
        )

    def fcf(day):
        cfo, capex = fact("cfo", day), fact("capex", day)
        return cfo - capex if cfo is not None and capex is not None else None

    def parent_equity(day):
        record = selected.get(("equity", day, "instant"))
        if record and record.get("source_tag") == "StockholdersEquity":
            return fact("equity", day, "instant")
        return None

    def verified_debt(day):
        record = selected.get(("total_debt", day, "instant"))
        if record and record.get("source_tag") == "DebtLongtermAndShorttermCombinedAmount":
            return fact("total_debt", day, "instant")
        return None

    def invested(day):
        components = [
            verified_debt(day),
            parent_equity(day),
            fact("noncontrolling_interest", day, "instant"),
            fact("cash", day, "instant"),
        ]
        return (
            components[0] + components[1] + components[2] - components[3]
            if all(v is not None for v in components)
            else None
        )

    def year_pair(first, second):
        return 300 <= (first - second).days <= 430

    if annual and (as_of - annual[0]).days <= 550:
        day = annual[0]
        revenue, income, operating = (
            fact("revenue", day),
            fact("net_income", day),
            fact("operating_income", day),
        )
        free_cash = fcf(day)
        values.update(
            net_margin=divide(income, revenue),
            operating_margin=divide(operating, revenue),
            fcf_margin=divide(free_cash, revenue),
            cash_flow_quality=divide(fact("cfo", day), income),
            earnings_yield=divide(income, market_cap),
            fcf_yield=divide(free_cash, market_cap),
            price_to_sales=divide(market_cap, revenue),
        )
        equity = parent_equity(day)
        values["price_to_book"] = divide(market_cap, equity)
        if len(annual) > 1 and year_pair(day, annual[1]):
            previous = annual[1]
            values.update(
                revenue_growth=divide(revenue, fact("revenue", previous)),
                earnings_growth=divide(income, fact("net_income", previous)),
                fcf_growth=divide(free_cash, fcf(previous)),
            )
            for key in ("revenue_growth", "earnings_growth", "fcf_growth"):
                values[key] = values[key] - 1 if values[key] is not None else None
            previous_equity = parent_equity(previous)
            if equity is not None and previous_equity is not None:
                values["roe"] = divide(income, (equity + previous_equity) / 2)
            capital, old_capital = invested(day), invested(previous)
            tax_rate = divide(fact("income_tax", day), fact("pretax_income", day))
            if (
                capital is not None
                and old_capital is not None
                and tax_rate is not None
                and 0 <= tax_rate <= 1
                and operating is not None
            ):
                values["roic"] = divide(operating * (1 - tax_rate), (capital + old_capital) / 2)
            if len(annual) > 2 and year_pair(previous, annual[2]):
                prior_growth = divide(fact("revenue", previous), fact("revenue", annual[2]))
                if prior_growth is not None and values["revenue_growth"] is not None:
                    values["growth_acceleration"] = values["revenue_growth"] - (prior_growth - 1)
        debt = verified_debt(day)
        values["debt_to_assets"] = divide(debt, fact("total_assets", day, "instant"))
        da = fact("depreciation_amortization", day)
        claims = [
            market_cap,
            debt,
            fact("preferred_equity", day, "instant"),
            fact("noncontrolling_interest", day, "instant"),
            fact("cash", day, "instant"),
        ]
        if all(v is not None for v in claims) and operating is not None and da is not None:
            values["ev_to_operating_ebitda"] = divide(
                claims[0] + claims[1] + claims[2] + claims[3] - claims[4], operating + da
            )
        earnings = [fact("net_income", period) for period in annual[:5]]
        if (
            len(earnings) == 5
            and all(value is not None for value in earnings)
            and all(year_pair(a, b) for a, b in pairwise(annual[:5]))
        ):
            mean = sum(earnings) / 5
            values["earnings_variability"] = pstdev(earnings) / abs(mean) if mean else None
    return {
        "as_of": str(as_of),
        "annual_period": str(annual[0]) if annual else None,
        "factors": {
            key: {
                "value": value,
                "status": "complete" if value is not None else "insufficient_aligned_inputs",
                "definition": DEFINITIONS[key],
            }
            for key, value in values.items()
        },
        "limitations": [
            "Annual statements, not trailing twelve months.",
            "Unverified debt definitions, missing claims or nonpositive denominators are not imputed.",
            "Market value uses latest reported shares and can lag actual issuance/buybacks.",
        ],
    }
