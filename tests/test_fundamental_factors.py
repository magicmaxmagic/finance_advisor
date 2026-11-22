from datetime import date

import pytest

from finlab.analytics.fundamentals import calculate_fundamentals

ASOF = date(2022, 3, 1)


def observation(name, year, value, bucket="annual", source_tag="reported"):
    return dict(
        canonical=name,
        period_end=date(year, 12, 31),
        period_start=date(year, 1, 1),
        duration_bucket=bucket,
        value=value,
        filed_date=date(year + 1, 2, 1),
        accession_number=f"{year}",
        source_tag=source_tag,
    )


def test_ratios_use_matching_periods_and_average_equity():
    facts = []
    for year, revenue, income, equity in ((2020, 100, 10, 40), (2021, 120, 12, 60)):
        facts.extend(
            [
                observation("revenue", year, revenue),
                observation("net_income", year, income),
                observation("equity", year, equity, "instant", "StockholdersEquity"),
            ]
        )
    facts.extend(
        [
            observation("cfo", 2021, 20),
            observation("capex", 2021, 5),
            observation("revenue", 2022, 9999),
        ]
    )
    report = calculate_fundamentals(facts, ASOF, market_cap=240)
    values = {key: row["value"] for key, row in report["factors"].items()}
    assert values["net_margin"] == pytest.approx(0.1)
    assert values["revenue_growth"] == pytest.approx(0.2)
    assert values["roe"] == pytest.approx(12 / 50)
    assert values["fcf_yield"] == pytest.approx(15 / 240)
    assert values["price_to_book"] == 4
    assert values["ev_to_operating_ebitda"] is None


def test_negative_growth_base_and_missing_periods_are_unavailable():
    facts = [
        observation("revenue", 2021, 120),
        observation("net_income", 2021, 10),
        observation("revenue", 2020, 100),
        observation("net_income", 2020, -5),
        observation("capex", 2021, 1),
    ]
    values = calculate_fundamentals(facts, ASOF)["factors"]
    assert values["earnings_growth"]["value"] is None
    assert values["fcf_yield"]["status"] == "insufficient_aligned_inputs"


def test_profitable_multiple_requires_positive_denominator_and_market_value():
    facts = [observation("revenue", 2021, 100), observation("net_income", 2021, -5)]
    values = calculate_fundamentals(facts, ASOF, market_cap=200)["factors"]
    assert values["trailing_pe"]["value"] is None
    assert values["earnings_yield"]["value"] is None


def test_enterprise_value_requires_all_claims_and_aligned_positive_ebitda():
    facts = [
        observation("revenue", 2021, 100),
        observation("net_income", 2021, 10),
        observation("operating_income", 2021, 20),
        observation("depreciation_amortization", 2021, 5),
        observation("total_debt", 2021, 35, "instant", "DebtLongtermAndShorttermCombinedAmount"),
        observation("preferred_equity", 2021, 2, "instant"),
        observation("noncontrolling_interest", 2021, 3, "instant"),
        observation("cash", 2021, 10, "instant", "CashAndCashEquivalentsAtCarryingValue"),
    ]
    factors = calculate_fundamentals(facts, ASOF, 100)["factors"]
    assert factors["trailing_pe"]["value"] == 10
    assert factors["ev_to_operating_ebitda"]["value"] == pytest.approx(130 / 25)
    assert factors["ev_to_ebit"]["value"] == pytest.approx(130 / 20)
    assert factors["ev_to_revenue"]["value"] == pytest.approx(130 / 100)
    assert factors["net_debt_to_ebitda"]["value"] == 1
    assert calculate_fundamentals(facts[:-1], ASOF, 100)["factors"]["ev_to_ebit"]["value"] is None
    financial = calculate_fundamentals(facts, ASOF, 100, financial_issuer=True)["factors"]
    assert financial["ev_to_operating_ebitda"]["status"] == "not_applicable"
    assert financial["net_debt_to_ebitda"]["status"] == "not_applicable"


def test_reject_wrong_fiscal_start_and_future_restatement():
    original = observation("net_income", 2021, 12)
    revised = {**original, "value": 36, "filed_date": date(2022, 4, 1)}
    revenues = observation("revenue", 2021, 100)
    inconsistent = {**observation("cfo", 2021, 40), "period_start": date(2021, 2, 1)}
    facts = [original, revised, revenues, inconsistent, observation("capex", 2021, 7)]
    factors = calculate_fundamentals(facts, ASOF, 120)["factors"]
    assert factors["trailing_pe"]["value"] == 10
    assert factors["price_to_fcf"]["value"] is None


def test_ttm_requires_four_contiguous_public_quarters():
    facts = []
    starts = [(1, 1), (4, 1), (7, 1), (10, 1)]
    ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    for index, ((month, day), (end_month, end_day)) in enumerate(zip(starts, ends, strict=True)):
        facts.append(
            {
                **observation("net_income", 2021, index + 1, "quarter", "NetIncomeLoss"),
                "period_start": date(2021, month, day),
                "period_end": date(2021, end_month, end_day),
            }
        )
    factors = calculate_fundamentals(facts, ASOF, 100)["factors"]
    assert factors["ttm_pe"]["value"] == 10
    assert factors["ttm_earnings_yield"]["value"] == 0.1
    assert factors["trailing_pe"]["value"] is None
    facts[-1]["filed_date"] = date(2022, 3, 2)
    assert calculate_fundamentals(facts, ASOF, 100)["factors"]["ttm_pe"]["value"] is None


def test_roic_and_reported_debt_use_separate_supported_definitions():
    facts = []
    for year in (2020, 2021):
        facts += [
            observation("revenue", year, 100),
            observation("operating_income", year, 20),
            observation("income_tax", year, 5),
            observation("pretax_income", year, 20),
            observation("equity", year, 50, "instant", "StockholdersEquity"),
            observation(
                "total_debt", year, 50, "instant", "DebtLongtermAndShorttermCombinedAmount"
            ),
            observation("noncontrolling_interest", year, 10, "instant"),
            observation("cash", year, 10, "instant", "CashAndCashEquivalentsAtCarryingValue"),
            observation("total_assets", year, 150, "instant"),
            observation("debt_current_maturities", year, 5, "instant"),
            observation("debt_noncurrent", year, 40, "instant"),
            observation("commercial_paper", year, 3, "instant"),
        ]
    result = calculate_fundamentals(facts, ASOF)["factors"]
    assert result["roic"]["value"] == pytest.approx(20 * 0.75 / 100)
    assert result["reported_debt_to_assets"]["value"] == pytest.approx(48 / 150)
    assert result["debt_to_assets"]["value"] == pytest.approx(50 / 150)
    facts[-1]["accession_number"] = "different-filing"
    assert (
        calculate_fundamentals(facts, ASOF)["factors"]["reported_debt_to_assets"]["value"] is None
    )
