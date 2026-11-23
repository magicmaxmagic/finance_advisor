"""Prespecified research factors and score ablations, isolated from live scoring."""

from datetime import date, timedelta
from math import isfinite

EXTRA_FACTORS = {
    "ttm_pe": -1,
    "earnings_yield": 1,
    "historical_ttm_pe_percentile": -1,
    "roe": 1,
    "operating_margin": 1,
    "fcf_margin": 1,
    "earnings_variability": -1,
    "fcf_growth": 1,
    "ebitda_growth": 1,
    "relative_momentum_12m": 1,
    "rsi": 1,
    "macd_to_price": 1,
    "beta": -1,
    "downside_volatility": -1,
}
EXTRA_MARKET = {"relative_momentum_12m", "rsi", "macd_to_price", "beta", "downside_volatility"}
ABLATIONS = {"value_only": "valuation", "quality_only": "business_quality", "growth_only": "growth"}


def historical_ttm_percentile(value: float | None, previous: list, at: date) -> float | None:
    cutoff = at - timedelta(days=5 * 365)
    eligible = [
        amount for day, amount in previous if cutoff <= day < at and amount > 0 and isfinite(amount)
    ]
    if value is None or value <= 0 or not isfinite(value) or len(eligible) < 8:
        return None
    return (
        sum(amount < value for amount in eligible)
        + 0.5 * sum(amount == value for amount in eligible)
    ) / len(eligible)


def operating_ebitda_growth(observations: list, latest_fiscal: str | None) -> float | None:
    if not latest_fiscal:
        return None
    latest = date.fromisoformat(latest_fiscal)
    periods = {}
    for row in observations:
        if (
            row["canonical"] in {"operating_income", "depreciation_amortization"}
            and row["period_start"]
        ):
            end, start = map(date.fromisoformat, (row["period_end"], row["period_start"]))
            if 350 <= (end - start).days <= 380:
                periods.setdefault((end, start, row["accession_number"]), {})[row["canonical"]] = (
                    row["value"]
                )
    this_year = [
        values for (end, _, _), values in periods.items() if end == latest and len(values) == 2
    ]
    prior_year = [
        values
        for (end, _, _), values in periods.items()
        if 350 <= (latest - end).days <= 380 and len(values) == 2
    ]
    if len(this_year) != 1 or len(prior_year) != 1:
        return None
    current, prior = sum(this_year[0].values()), sum(prior_year[0].values())
    return current / prior - 1 if prior > 0 else None


def additional_observations(row: dict, raw: dict, history: list, at: date) -> dict:
    ttm_pe = raw.get("ttm_pe")
    percentile = historical_ttm_percentile(ttm_pe, history, at)
    if ttm_pe is not None and isfinite(ttm_pe) and ttm_pe > 0:
        history.append((at, ttm_pe))
    latest, macd = row["latest_price"], raw.get("macd")
    fundamental = row["fundamental_factors"]
    return {
        "historical_ttm_pe_percentile": percentile,
        "macd_to_price": macd / latest if latest and latest > 0 and macd is not None else None,
        "ebitda_growth": operating_ebitda_growth(
            fundamental["filing_evidence"], fundamental["annual_period"]
        )
        if not row["financial_issuer"]
        else None,
    }
