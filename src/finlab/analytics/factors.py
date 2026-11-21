"""Offline, PIT-compatible market factor calculations."""

from __future__ import annotations

from datetime import date

import polars as pl

FACTOR_CALCULATION_VERSION = "market-factors-v1"


def calculate_market_factors(
    prices: pl.DataFrame,
    benchmark: pl.DataFrame,
    as_of: date,
) -> pl.DataFrame:
    """Calculate descriptive market factors for rows observed on or before as_of.

    This module only reads supplied local frames. It does not download data or
    open DuckDB. All formulas use completed daily observations as an explicit
    input set; no provider and no execution-price API is called.
    """
    required = {"security_id", "date", "close"}
    if not required.issubset(prices.columns):
        raise ValueError(f"price frame requires columns {sorted(required)}")
    if not {"security_id", "date", "close"}.issubset(benchmark.columns):
        raise ValueError("benchmark frame requires security_id, date, close")

    cut = prices.filter(pl.col("date") <= as_of).sort(["security_id", "date"])
    if cut.is_empty():
        return pl.DataFrame(
            schema={
                "security_id": pl.Utf8,
                "as_of": pl.Date,
                "factor": pl.Utf8,
                "value": pl.Float64,
                "calculation_version": pl.Utf8,
                "data_status": pl.Utf8,
            }
        )

    frames: list[pl.DataFrame] = []
    for security_id in cut["security_id"].unique().to_list():
        frame = cut.filter(pl.col("security_id") == security_id).sort("date")
        if frame.height < 2:
            continue

        closes = frame["close"]
        latest = float(closes[-1])
        trading_days = frame.height
        lookbacks = {
            1: min(21, trading_days - 1),
            3: min(63, trading_days - 1),
            6: min(126, trading_days - 1),
            12: min(252, trading_days - 1),
        }
        returns: dict[str, float] = {}
        for months, window in lookbacks.items():
            past = float(closes[-window - 1])
            returns[f"return_{months}m"] = latest / past - 1 if past > 0 else None

        window_12 = lookbacks[12]
        past_12 = float(closes[-window_12 - 1])
        past_1 = float(closes[-22]) if trading_days > 22 else past_12
        returns["momentum_12_minus_1"] = (
            latest / past_12 - latest / past_1 if past_12 > 0 and past_1 > 0 else None
        )

        daily = frame["close"] / frame["close"].shift(1) - 1
        daily = daily.drop_nulls()
        volatility = float(daily.std(ddof=1) * (252**0.5)) if daily.len() > 1 else None
        peak = frame["close"].cum_max()
        drawdown = float((frame["close"] / peak - 1).min())

        ma50 = float(closes.tail(min(50, trading_days)).mean())
        ma200 = float(closes.tail(min(200, trading_days)).mean())
        rsi = _rsi(closes)
        macd = _macd(closes)

        values = {
            **returns,
            "annualized_volatility": volatility,
            "max_drawdown": drawdown,
            "ma50": ma50,
            "ma200": ma200,
            "rsi": rsi,
            "macd": macd,
        }
        frames.append(
            pl.DataFrame(
                {
                    "security_id": [security_id] * len(values),
                    "as_of": [as_of] * len(values),
                    "factor": list(values),
                    "value": list(values.values()),
                    "calculation_version": [FACTOR_CALCULATION_VERSION] * len(values),
                    "data_status": [
                        "complete" if all(v is not None for v in values.values()) else "partial"
                    ]
                    * len(values),
                }
            )
        )

    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed")


def _rsi(closes: pl.Series, window: int = 14) -> float | None:
    if closes.len() <= window:
        return None
    delta = closes.diff().drop_nulls()
    gains = delta.clip(lower_bound=0).tail(window)
    losses = (-delta.clip(upper_bound=0)).tail(window)
    average_gain = float(gains.mean())
    average_loss = float(losses.mean())
    if average_loss == 0:
        return 100.0
    rs = average_gain / average_loss
    return 100 - (100 / (1 + rs))


def _macd(closes: pl.Series) -> float | None:
    if closes.len() < 26:
        return None
    ema12 = closes.ewm_mean(span=12, adjust=False)
    ema26 = closes.ewm_mean(span=26, adjust=False)
    macd_series = ema12 - ema26
    signal = macd_series.ewm_mean(span=9, adjust=False)
    return float((macd_series - signal)[-1])
