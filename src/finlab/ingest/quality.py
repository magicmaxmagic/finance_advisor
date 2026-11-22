"""Fail-closed validation at the market observation ingestion boundary."""

from datetime import date

import polars as pl


class PriceQualityError(ValueError):
    """Incoming observations violate the warehouse price contract."""


def validate_daily_prices(frame: pl.DataFrame, start: date, end: date) -> None:
    required = {"symbol", "date", "open", "high", "low", "close", "volume", "source"}
    if not required.issubset(frame.columns):
        raise PriceQualityError(f"Missing price fields: {sorted(required - set(frame.columns))}")
    if frame.is_empty():
        return
    if frame.select(pl.struct("symbol", "date").is_duplicated().any()).item():
        raise PriceQualityError("Duplicate symbol/date observations")
    if frame.select(
        pl.any_horizontal([pl.col(c).is_null() for c in sorted(required)]).any()
    ).item():
        raise PriceQualityError("Null price observations")
    invalid = (
        pl.any_horizontal(
            [~pl.col(c).is_finite() | (pl.col(c) <= 0) for c in ("open", "high", "low", "close")]
        )
        | (pl.col("high") < pl.max_horizontal("open", "close", "low"))
        | (pl.col("low") > pl.min_horizontal("open", "close", "high"))
        | (pl.col("volume") < 0)
        | ~pl.col("date").is_between(start, end)
        | (pl.col("source").str.len_chars() == 0)
    )
    if frame.filter(invalid).height:
        raise PriceQualityError("Invalid OHLCV, provenance or observation date")
