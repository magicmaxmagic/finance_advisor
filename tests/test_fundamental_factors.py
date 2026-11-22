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
