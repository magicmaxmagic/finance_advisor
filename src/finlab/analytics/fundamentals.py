"""Aligned annual fundamentals; unsupported denominators remain unavailable."""

from datetime import date
from itertools import pairwise
from math import isfinite
from statistics import pstdev

DEFINITIONS = {
    "equity_to_assets": "Latest aligned fiscal-year parent book equity / total assets. Accounting capital proxy, not a regulatory capital ratio.",
    "net_margin": "Annual parent net income / annual revenue, same reporting period.",
    "ttm_pe": "Reported-share market cap divided by positive net income summed over four contiguous reported fiscal quarters.",
    "ttm_earnings_yield": "Positive net income summed over four contiguous reported fiscal quarters divided by reported-share market cap.",
    "trailing_pe": "Reported-share market capitalization / positive latest published fiscal-year parent net income. Historical API key; fiscal-year multiple.",
    "gross_margin": "(Revenue - reported cost of revenue) / revenue.",
    "ebitda_margin": "(Operating income + reported D&A) / revenue.",
    "roa": "Annual net income / average total assets.",
    "ev_to_ebit": "(Market capitalization + verified debt + preferred equity + NCI - cash) / operating income.",
    "ev_to_revenue": "Fully supported enterprise value / positive annual revenue.",
    "price_to_fcf": "Market capitalization / positive annual free cash flow.",
    "net_debt_to_ebitda": "(Verified debt - cash) / positive operating income plus D&A.",
    "debt_to_equity": "Verified debt / positive parent equity.",
    "cash_to_debt": "Cash / verified debt.",
    "current_ratio": "Current assets / current liabilities.",
    "interest_coverage": "Operating income / reported interest expense.",
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
    "reported_debt_to_assets": "(Current term debt + noncurrent term debt + commercial paper from one filing) / assets. Reported categories only.",
    "margin_change": "Current fiscal-year net margin less prior fiscal-year net margin.",
    "debt_to_assets": "Verified combined interest-bearing debt / same-date positive assets.",
}


def calculate_fundamentals(
    observations: list[dict],
    as_of: date,
    market_cap: float | None = None,
    *,
    financial_issuer: bool = False,
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
        if row and bucket == "annual":
            revenue_row = selected.get(("revenue", day, "annual"))
            if (
                not revenue_row
                or row["period_start"] is None
                or row["period_start"] != revenue_row["period_start"]
            ):
                return None
            if not 350 <= (day - row["period_start"]).days <= 380:
                return None
        value = row["value"] if row else None
        return value if value is not None and isfinite(value) else None

    def strict_cash(day):
        record = selected.get(("cash", day, "instant"))
        if record and record["source_tag"] == "CashAndCashEquivalentsAtCarryingValue":
            return fact("cash", day, "instant")
        return None

    def divide(numerator, denominator):
        return (
            numerator / denominator
            if numerator is not None and denominator is not None and denominator > 0
            else None
        )

    def fcf(day):
        cfo, capex = fact("cfo", day), fact("capex", day)
        return cfo - capex if cfo is not None and capex is not None and capex >= 0 else None

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
            strict_cash(day),
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
        equity = parent_equity(day)
        values.update(
            equity_to_assets=divide(equity, fact("total_assets", day, "instant")),
            net_margin=divide(income, revenue),
            operating_margin=divide(operating, revenue),
            fcf_margin=divide(free_cash, revenue),
            cash_flow_quality=divide(fact("cfo", day), income),
            earnings_yield=divide(income, market_cap)
            if income is not None and income > 0
            else None,
            fcf_yield=divide(free_cash, market_cap),
            price_to_sales=divide(market_cap, revenue),
            trailing_pe=divide(market_cap, income) if income is not None and income > 0 else None,
            gross_margin=(revenue - fact("cogs", day)) / revenue
            if revenue is not None and revenue > 0 and fact("cogs", day) is not None
            else None,
            ebitda_margin=divide(
                (operating + fact("depreciation_amortization", day))
                if operating is not None and fact("depreciation_amortization", day) is not None
                else None,
                revenue,
            ),
            roa=divide(
                income,
                (fact("total_assets", day, "instant") + fact("total_assets", annual[1], "instant"))
                / 2,
            )
            if len(annual) > 1
            and year_pair(day, annual[1])
            and fact("total_assets", day, "instant") is not None
            and fact("total_assets", annual[1], "instant") is not None
            else None,
            price_to_fcf=divide(market_cap, free_cash)
            if free_cash is not None and free_cash > 0
            else None,
            net_debt_to_ebitda=divide(
                verified_debt(day) - strict_cash(day),
                operating + fact("depreciation_amortization", day),
            )
            if verified_debt(day) is not None
            and strict_cash(day) is not None
            and operating is not None
            and fact("depreciation_amortization", day) is not None
            else None,
            debt_to_equity=divide(verified_debt(day), equity)
            if verified_debt(day) is not None and equity is not None
            else None,
            cash_to_debt=divide(strict_cash(day), verified_debt(day))
            if verified_debt(day) is not None
            else None,
            current_ratio=divide(
                fact("current_assets", day, "instant"), fact("current_liabilities", day, "instant")
            ),
            interest_coverage=divide(operating, fact("interest_expense", day)),
        )
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
            old_margin = divide(fact("net_income", previous), fact("revenue", previous))
            if values["net_margin"] is not None and old_margin is not None:
                values["margin_change"] = values["net_margin"] - old_margin
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
            strict_cash(day),
        ]
        if all(v is not None for v in claims):
            ev = sum(claims[:4]) - claims[4]
            if ev > 0:
                values["ev_to_operating_ebitda"] = (
                    divide(ev, operating + da) if operating is not None and da is not None else None
                )
                values["ev_to_ebit"] = divide(ev, operating)
                values["ev_to_revenue"] = divide(ev, revenue)
        debt_parts = ("debt_current_maturities", "debt_noncurrent", "commercial_paper")
        records = [selected.get((key, day, "instant")) for key in debt_parts]
        parts = [fact(key, day, "instant") for key in debt_parts]
        if (
            all(v is not None and v >= 0 for v in parts)
            and len({r["accession_number"] for r in records}) == 1
        ):
            values["reported_debt_to_assets"] = divide(
                sum(parts), fact("total_assets", day, "instant")
            )
        earnings = [fact("net_income", period) for period in annual[:5]]
        if (
            len(earnings) == 5
            and all(value is not None for value in earnings)
            and all(year_pair(a, b) for a, b in pairwise(annual[:5]))
        ):
            mean = sum(earnings) / 5
            values["earnings_variability"] = pstdev(earnings) / abs(mean) if mean else None
    nonapplicable = (
        {
            "ev_to_operating_ebitda",
            "ev_to_ebit",
            "ev_to_revenue",
            "roic",
            "gross_margin",
            "ebitda_margin",
            "net_debt_to_ebitda",
            "debt_to_equity",
            "debt_to_assets",
            "reported_debt_to_assets",
            "interest_coverage",
            "current_ratio",
            "cash_to_debt",
            "fcf_margin",
            "fcf_yield",
            "price_to_fcf",
            "cash_flow_quality",
            "fcf_growth",
        }
        if financial_issuer
        else set()
    )
    for key in nonapplicable:
        values[key] = None
    ttm, quarters = trailing_income(list(selected.values()), as_of)
    if ttm is not None and ttm > 0:
        values["ttm_pe"] = divide(market_cap, ttm)
        values["ttm_earnings_yield"] = divide(ttm, market_cap)
    return {
        "as_of": str(as_of),
        "period_type": "latest_published_fiscal_year",
        "source": "SEC companyfacts",
        "ttm_quarter_evidence": [
            {**row, **{key: str(row[key]) for key in ("period_start", "period_end", "filed_date")}}
            for row in quarters
        ],
        "filing_evidence": [
            {
                key: str(row[key])
                if key in {"period_start", "period_end", "filed_date"} and row[key] is not None
                else row[key]
                for key in (
                    "canonical",
                    "source_tag",
                    "period_start",
                    "period_end",
                    "filed_date",
                    "accession_number",
                    "value",
                )
            }
            for row in sorted(
                selected.values(),
                key=lambda row: (row["period_end"], row["canonical"]),
                reverse=True,
            )
            if row["period_end"] in annual[:5] and row["duration_bucket"] in {"annual", "instant"}
        ],
        "annual_period": str(annual[0]) if annual else None,
        "factors": {
            key: {
                "value": value,
                "status": "not_applicable"
                if key in nonapplicable
                else "complete"
                if value is not None
                else "insufficient_aligned_inputs",
                "definition": DEFINITIONS[key],
            }
            for key, value in values.items()
        },
        "limitations": [
            "Fiscal-year ratios use annual statements. Explicit TTM ratios require four contiguous reported quarters.",
            "Unverified debt definitions, missing claims or nonpositive denominators are not imputed.",
            "Market value uses latest reported shares and can lag actual issuance/buybacks.",
        ],
    }


def trailing_income(observations, as_of):
    """Sum four contiguous, individually reported quarters. No Q4 imputation."""
    quarters = sorted(
        (
            row
            for row in observations
            if row["canonical"] == "net_income"
            and row["duration_bucket"] == "quarter"
            and row["filed_date"] <= as_of
            and row["period_end"] <= as_of
        ),
        key=lambda row: row["period_end"],
        reverse=True,
    )[:4]
    if len(quarters) != 4 or (as_of - quarters[0]["period_end"]).days > 180:
        return None, []
    if any(
        not row["period_start"]
        or not 70 <= (row["period_end"] - row["period_start"]).days <= 110
        or not isfinite(row["value"])
        for row in quarters
    ):
        return None, []
    if any(
        (newer["period_start"] - older["period_end"]).days != 1
        for newer, older in pairwise(quarters)
    ):
        return None, []
    if (
        len({row["source_tag"] for row in quarters}) != 1
        or not 350 <= (quarters[0]["period_end"] - quarters[-1]["period_start"]).days <= 380
    ):
        return None, []
    return sum(row["value"] for row in quarters), quarters
