"""Offline tests for the Analytics market-factor engine."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from finlab.analytics.factors import calculate_market_factors


def test_market_factors_are_deterministic_and_point_in_time():
    as_of = date(2026, 11, 21)
    days = [as_of - timedelta(days=days_back) for days_back in range(300, -1, -1)]
    close = [100.0 + (index * 0.15) for index in range(len(days))]
    prices = pl.DataFrame(
        {
            "security_id": ["US:TEST"] * len(days),
            "date": days,
            "close": close,
        }
    )
    benchmark = prices.with_columns(pl.lit("US:BENCHMARK").alias("security_id"))

    one = calculate_market_factors(prices, benchmark, as_of)
    two = calculate_market_factors(prices, benchmark, as_of)

    assert one.equals(two)
    assert set(one["factor"])
    assert set(one["as_of"]) == {as_of}
    assert one.filter(pl.col("factor") == "return_12m")["value"][0] > 0


def test_insufficient_history_returns_an_explicit_empty_frame():
    as_of = date(2026, 11, 21)
    prices = pl.DataFrame(
        {
            "security_id": ["US:TEST"],
            "date": [as_of],
            "close": [100.0],
        }
    )
    benchmark = prices.clone()

    result = calculate_market_factors(prices, benchmark, as_of)
    assert result.is_empty()


def test_invalid_schema_is_refused():
    prices = pl.DataFrame({"security_id": ["US:TEST"], "date": [date(2026, 11, 21)]})
    benchmark = pl.DataFrame(
        {"security_id": ["US:BENCHMARK"], "date": [date(2026, 11, 21)], "close": [100.0]}
    )

    try:
        calculate_market_factors(prices, benchmark, date(2026, 11, 21))
    except ValueError as exc:
        assert "close" in str(exc)
    else:
        raise AssertionError("invalid price schema should fail closed")


def test_short_history_never_impersonates_twelve_month_returns():
    prices = pl.DataFrame(
        {
            "security_id": ["A"] * 22,
            "date": [date(2024, 1, 1) + timedelta(days=i) for i in range(22)],
            "close": [100.0 + i for i in range(22)],
        }
    )
    result = calculate_market_factors(prices, prices, date(2024, 2, 1))
    values = dict(zip(result["factor"], result["value"], strict=True))
    assert values["return_1m"] == pytest.approx(0.21)
    assert values["return_12m"] is None
    assert values["momentum_12_minus_1"] is None
    assert values["ma200"] is None


def test_skip_month_momentum_and_relative_benchmark():
    import pytest

    prices = pl.DataFrame(
        {
            "security_id": ["A"] * 253,
            "date": [date(2024, 1, 1) + timedelta(days=i) for i in range(253)],
            "close": [100.0 + i for i in range(253)],
        }
    )
    benchmark = prices.with_columns(pl.lit(100.0).alias("close"))
    result = calculate_market_factors(prices, benchmark, date(2025, 1, 1))
    values = dict(zip(result["factor"], result["value"], strict=True))
    assert values["momentum_12_minus_1"] == pytest.approx(331 / 100 - 1)
    assert values["relative_momentum_12m"] == pytest.approx(352 / 100 - 1)


def test_invalid_price_is_rejected():
    import pytest

    prices = pl.DataFrame({"security_id": ["A"], "date": [date(2024, 1, 1)], "close": [0.0]})
    with pytest.raises(ValueError, match="finite and positive"):
        calculate_market_factors(prices, prices, date(2024, 1, 1))


def test_market_beta_matches_identical_benchmark_and_risk_metrics_exist():
    prices = pl.DataFrame(
        {
            "security_id": ["A"] * 5,
            "date": [date(2020, 1, 1) + timedelta(days=i) for i in range(5)],
            "close": [100.0, 110.0, 105.0, 120.0, 115.0],
        }
    )
    result = calculate_market_factors(prices, prices, date(2020, 2, 1))
    factors = dict(zip(result["factor"], result["value"], strict=True))
    assert factors["beta"] == pytest.approx(1)
    assert factors["downside_volatility"] > 0
    assert factors["cvar_95"] >= factors["var_95"]
