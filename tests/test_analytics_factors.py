"""Offline tests for the Analytics market-factor engine."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

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
