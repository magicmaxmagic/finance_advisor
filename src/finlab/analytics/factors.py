"""Offline, PIT-compatible market factor calculations."""

from __future__ import annotations

from datetime import date

import polars as pl

from finlab.research.stats import performance_metrics

FACTOR_CALCULATION_VERSION = "market-factors-v2"


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

    for supplied in (prices, benchmark):
        eligible = supplied.filter(pl.col("date") <= as_of)
        if eligible.select(pl.struct("security_id", "date").is_duplicated().any()).item():
            raise ValueError("Duplicate security/date prices")
        if eligible.filter(
            pl.col("close").is_null() | ~pl.col("close").is_finite() | (pl.col("close") <= 0)
        ).height:
            raise ValueError("Prices must be finite and positive")
    if benchmark["security_id"].n_unique() > 1:
        raise ValueError("Supply one benchmark security")

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
            1: 21,
            3: 63,
            6: 126,
            12: 252,
        }
        returns: dict[str, float] = {}
        for months, window in lookbacks.items():
            if trading_days <= window:
                returns[f"return_{months}m"] = None
                continue
            past = float(closes[-window - 1])
            returns[f"return_{months}m"] = latest / past - 1 if past > 0 else None

        returns["momentum_12_minus_1"] = (
            float(closes[-22]) / float(closes[-253]) - 1 if trading_days >= 253 else None
        )
        market = benchmark.filter(pl.col("date") <= as_of).sort("date")
        returns["relative_momentum_12m"] = None
        if (
            market.height >= 253
            and trading_days >= 253
            and market["date"][-1] == frame["date"][-1]
            and market["date"][-253] == frame["date"][-253]
        ):
            returns["relative_momentum_12m"] = returns["return_12m"] - (
                float(market["close"][-1]) / float(market["close"][-253]) - 1
            )

        daily = frame["close"] / frame["close"].shift(1) - 1
        daily = daily.drop_nulls()
        volatility = float(daily.std(ddof=1) * (252**0.5)) if daily.len() > 1 else None
        peak = frame["close"].cum_max()
        drawdown = float((frame["close"] / peak - 1).min())

        ma50 = float(closes.tail(50).mean()) if trading_days >= 50 else None
        ma200 = float(closes.tail(200).mean()) if trading_days >= 200 else None
        rsi = _rsi(closes)
        macd = _macd(closes)

        risk = {
            key: None
            for key in ("sharpe", "sortino", "var_95", "cvar_95", "beta", "downside_volatility")
        }
        if daily.len() > 1:
            summary = performance_metrics(daily.to_numpy())
            risk.update({key: summary[key] for key in ("sharpe", "sortino", "var_95", "cvar_95")})
            downside = daily.clip(upper_bound=0)
            risk["downside_volatility"] = float((downside.pow(2).mean() * 252) ** 0.5)
        matched = (
            frame.select("date", "close")
            .join(
                market.select("date", pl.col("close").alias("benchmark_close")),
                on="date",
                how="inner",
            )
            .sort("date")
        )
        if matched.height > 2 and matched.height == frame.height:
            asset_returns = (matched["close"] / matched["close"].shift(1) - 1).drop_nulls()
            benchmark_returns = (
                matched["benchmark_close"] / matched["benchmark_close"].shift(1) - 1
            ).drop_nulls()
            risk["beta"] = performance_metrics(
                asset_returns.to_numpy(), benchmark_returns.to_numpy()
            ).get("beta")
        values = {
            **risk,
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
                        "complete" if value is not None else "insufficient_data"
                        for value in values.values()
                    ],
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
        return 50.0 if average_gain == 0 else 100.0
    rs = average_gain / average_loss
    return 100 - (100 / (1 + rs))


def _macd(closes: pl.Series) -> float | None:
    if closes.len() < 26:
        return None
    ema12 = closes.ewm_mean(span=12, adjust=False)
    ema26 = closes.ewm_mean(span=26, adjust=False)
    macd_series = ema12 - ema26
    return float(macd_series[-1])
