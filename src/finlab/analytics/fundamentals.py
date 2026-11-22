"""Aligned annual fundamentals; unsupported denominators remain unavailable."""

from datetime import date, timedelta
from itertools import pairwise
from math import isfinite
from statistics import pstdev

DEFINITIONS = {
    "equity_to_assets": "Latest aligned fiscal-year parent book equity / total assets. Accounting capital proxy, not a regulatory capital ratio.",
    "net_margin": "Annual parent net income / annual revenue, same reporting period.",
    "ttm_pe": "Reported-share market cap divided by positive trailing net income from four contiguous quarters or an aligned fiscal-year and YTD bridge.",
    "ttm_earnings_yield": "Positive trailing net income from four contiguous quarters or an aligned fiscal-year and YTD bridge divided by reported-share market cap.",
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
    "nonoperating_interest_coverage": "Operating income / separately reported gross nonoperating interest expense.",
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

MARKET_VALUE_RATIOS = {
    "trailing_pe",
    "earnings_yield",
    "ttm_pe",
    "ttm_earnings_yield",
    "price_to_fcf",
    "fcf_yield",
    "price_to_sales",
    "price_to_book",
    "ev_to_ebit",
    "ev_to_revenue",
    "ev_to_operating_ebitda",
}


def diagnostic(code, message):
    return {"code": code, "message": message}


def calculate_fundamentals(
    observations: list[dict],
    as_of: date,
    market_cap: float | None = None,
    *,
    financial_issuer: bool = False,
    market_cap_diagnostic=None,
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
        minority = selected.get(("noncontrolling_interest", day, "instant"))
        if (
            record
            and record.get("source_tag")
            == "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
            and minority
            and minority.get("source_tag") == "MinorityInterest"
            and record["accession_number"]
            and record["accession_number"] == minority["accession_number"]
            and record["filed_date"] == minority["filed_date"]
            and isfinite(record["value"])
            and isfinite(minority["value"])
        ):
            return record["value"] - minority["value"]
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
            nonoperating_interest_coverage=divide(
                operating, fact("nonoperating_interest_expense", day)
            ),
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
            "nonoperating_interest_coverage",
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
    bridged, bridge = trailing_income_bridge(list(selected.values()), as_of)
    if bridge and (not quarters or bridge[0]["period_end"] > quarters[0]["period_end"]):
        ttm = bridged
        quarters = []
    else:
        bridge = []
    if ttm is not None and ttm > 0:
        values["ttm_pe"] = divide(market_cap, ttm)
        values["ttm_earnings_yield"] = divide(ttm, market_cap)

    def missing(key):
        if key in nonapplicable:
            return diagnostic(
                "NOT_APPLICABLE", "This ratio is outside the financial-issuer policy."
            )
        if key.startswith("ttm_"):
            if ttm is None:
                return diagnostic(
                    "INSUFFICIENT_HISTORY",
                    "Four aligned quarters or an exactly aligned fiscal-year and comparative YTD bridge are required.",
                )
            if ttm <= 0:
                return diagnostic(
                    "INVALID_DENOMINATOR", "The ratio requires positive trailing net income."
                )
        else:
            if not annual:
                return diagnostic(
                    "SOURCE_MISSING", "No published annual revenue period anchors this ratio."
                )
            day = annual[0]
            if (as_of - day).days > 550:
                return diagnostic(
                    "STALE_DATA", "The last annual reporting period ended over 550 days ago."
                )
            denominators = {
                "trailing_pe": "net_income",
                "earnings_yield": "net_income",
                "interest_coverage": "interest_expense",
                "nonoperating_interest_coverage": "nonoperating_interest_expense",
                "revenue_growth": "revenue",
                "earnings_growth": "net_income",
            }
            if key in denominators:
                name = denominators[key]
                prior = key in {"revenue_growth", "earnings_growth"}
                if prior and (len(annual) < 2 or not year_pair(day, annual[1])):
                    return diagnostic(
                        "INSUFFICIENT_HISTORY", "Two comparable annual periods are required."
                    )
                period = annual[1] if prior else day
                value = fact(name, period)
                if value is None:
                    code = (
                        "INVALID_FISCAL_ALIGNMENT"
                        if (name, period, "annual") in selected
                        else "SOURCE_MISSING"
                    )
                    return diagnostic(
                        code, f"An aligned {name.replace('_', ' ')} observation is required."
                    )
                if value <= 0:
                    return diagnostic(
                        "INVALID_DENOMINATOR",
                        f"The ratio requires positive {name.replace('_', ' ')}.",
                    )
            if key == "current_ratio":
                liabilities = fact("current_liabilities", day, "instant")
                if liabilities is not None and liabilities <= 0:
                    return diagnostic(
                        "INVALID_DENOMINATOR", "Current liabilities must be positive."
                    )
            if key in {"price_to_fcf", "price_to_book"}:
                denominator = fcf(day) if key == "price_to_fcf" else parent_equity(day)
                if denominator is not None and denominator <= 0:
                    return diagnostic(
                        "INVALID_DENOMINATOR",
                        "The ratio requires positive free cash flow or parent equity, respectively.",
                    )
        if key in MARKET_VALUE_RATIOS and market_cap is None:
            return market_cap_diagnostic or diagnostic(
                "SOURCE_MISSING", "A verified market value is required."
            )
        if not key.startswith("ttm_"):
            day = annual[0]
            if key.startswith("ev_to_"):
                if any(v is None for v in claims):
                    return diagnostic(
                        "SOURCE_MISSING", "Complete enterprise-value claims and cash are required."
                    )
                denominator = {
                    "ev_to_operating_ebitda": operating + da
                    if operating is not None and da is not None
                    else None,
                    "ev_to_revenue": revenue,
                    "ev_to_ebit": operating,
                }[key]
                if denominator is not None and (denominator <= 0 or sum(claims[:4]) <= claims[4]):
                    return diagnostic(
                        "INVALID_DENOMINATOR",
                        "Enterprise value and the selected earnings or sales basis must be positive.",
                    )
            if (
                key
                in {
                    "debt_to_assets",
                    "debt_to_equity",
                    "cash_to_debt",
                    "net_debt_to_ebitda",
                    "roic",
                }
                and verified_debt(day) is None
            ):
                record = selected.get(("total_debt", day, "instant"))
                if record:
                    return diagnostic(
                        "UNSUPPORTED",
                        f"{record['source_tag']} does not establish complete combined short- and long-term debt.",
                    )
                return diagnostic(
                    "SOURCE_MISSING",
                    "A verified combined short- and long-term debt observation is missing.",
                )
            if key == "reported_debt_to_assets" and all(record is not None for record in records):
                return diagnostic(
                    "INVALID_FISCAL_ALIGNMENT",
                    "Reported debt components must reconcile on the same date and filing.",
                )
            if key in {
                "roa",
                "roe",
                "growth_acceleration",
                "fcf_growth",
                "margin_change",
                "earnings_variability",
            }:
                required = {"growth_acceleration": 3, "earnings_variability": 5}.get(key, 2)
                if len(annual) < required or not all(
                    year_pair(a, b) for a, b in pairwise(annual[:required])
                ):
                    return diagnostic(
                        "INSUFFICIENT_HISTORY",
                        f"{required} comparable annual periods are required.",
                    )
        return diagnostic("SOURCE_MISSING", "The required aligned filing inputs are unavailable.")

    return {
        "as_of": str(as_of),
        "period_type": "latest_published_fiscal_year",
        "source": "SEC companyfacts",
        "ttm_quarter_evidence": [
            {**row, **{key: str(row[key]) for key in ("period_start", "period_end", "filed_date")}}
            for row in quarters
        ],
        "ttm_bridge_evidence": [
            {
                **row,
                "operation": operation,
                **{key: str(row[key]) for key in ("period_start", "period_end", "filed_date")},
            }
            for row, operation in zip(
                bridge,
                ("add_current_ytd", "add_prior_fiscal_year", "subtract_prior_ytd"),
                strict=False,
            )
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
                "diagnostic": missing(key) if value is None else None,
            }
            for key, value in values.items()
        },
        "limitations": [
            "Fiscal-year ratios use annual statements. TTM requires four contiguous reported quarters or an aligned fiscal-year bridge with comparative YTD in the same filing.",
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


def trailing_income_bridge(observations, as_of):
    """FY + current YTD - prior YTD with both YTD facts from one filing."""
    income = [
        row
        for row in observations
        if row["canonical"] == "net_income"
        and row["period_start"]
        and row["filed_date"] <= as_of
        and row["period_end"] <= as_of
        and isfinite(row["value"])
    ]
    annual = max(
        (row for row in income if row["duration_bucket"] == "annual"),
        key=lambda row: row["period_end"],
        default=None,
    )
    if annual is None or not 350 <= (annual["period_end"] - annual["period_start"]).days <= 380:
        return None, []
    ranges = {"quarter": (80, 100), "half": (170, 190), "ytd9": (260, 285)}
    current = max(
        (
            row
            for row in income
            if row["duration_bucket"] in ranges
            and row["period_start"] == annual["period_end"] + timedelta(days=1)
        ),
        key=lambda row: row["period_end"],
        default=None,
    )
    if (
        current is None
        or (as_of - current["period_end"]).days > 180
        or annual["filed_date"] > current["filed_date"]
    ):
        return None, []
    length = (current["period_end"] - current["period_start"]).days
    minimum, maximum = ranges[current["duration_bucket"]]
    if not minimum <= length <= maximum:
        return None, []
    comparable = [
        prior
        for prior in income
        if prior["period_start"] == annual["period_start"]
        and prior["duration_bucket"] == current["duration_bucket"]
        and prior["accession_number"] == current["accession_number"]
        and prior["filed_date"] == current["filed_date"]
        and minimum <= (prior["period_end"] - prior["period_start"]).days <= maximum
        and abs((prior["period_end"] - prior["period_start"]).days - length) <= 7
        and len({annual["source_tag"], current["source_tag"], prior["source_tag"]}) == 1
        and 350 <= (current["period_end"] - prior["period_end"]).days <= 380
    ]
    if len(comparable) != 1:
        return None, []
    prior = comparable[0]
    return annual["value"] + current["value"] - prior["value"], [current, annual, prior]
