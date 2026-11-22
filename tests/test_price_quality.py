from datetime import date

import polars as pl
import pytest

from finlab.ingest.quality import PriceQualityError, validate_daily_prices
from finlab.ingest.yahoo import YahooProvider


def bar():
    return dict(
        symbol="A",
        date=date(2024, 1, 2),
        open=10.0,
        high=11.0,
        low=9.0,
        close=10.0,
        volume=100,
        source="yahoo",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("close", float("inf")),
        ("open", 0.0),
        ("low", 12.0),
        ("volume", -1),
        ("source", ""),
        ("date", date(2025, 1, 1)),
    ],
)
def test_reject_invalid_observations(field, value):
    row = bar()
    row[field] = value
    with pytest.raises(PriceQualityError):
        validate_daily_prices(pl.DataFrame([row]), date(2024, 1, 1), date(2024, 12, 31))


def test_duplicate_observations_are_not_silently_resolved():
    with pytest.raises(PriceQualityError, match="Duplicate"):
        validate_daily_prices(pl.DataFrame([bar(), bar()]), date(2024, 1, 1), date(2024, 12, 31))


def test_adjusted_ohlc_requires_a_separate_contract():
    with pytest.raises(ValueError, match="Adjusted OHLC"):
        YahooProvider(auto_adjust=True)
