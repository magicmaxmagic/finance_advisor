"""Offline, PIT-compatible market factor calculations."""

from __future__ import annotations

from datetime import date

import polars as pl

from finlab.research.stats import performance_metrics

FACTOR_CALCULATION_VERSION = "market-factors-v3"

MARKET_DEFINITIONS = {
    "annualized_volatility": "Sample standard deviation of close-to-close simple returns times sqrt(252).",
    "max_drawdown": "Minimum close divided by its running peak, less one, over the supplied history.",
    "downside_volatility": "Square root of 252 times mean squared negative returns, including zeros on up days.",
    "beta": "Covariance of asset and SPY daily returns divided by SPY return variance.",
    "var_95": "Negative of the fifth percentile of daily simple returns, expressed as a loss.",
    "cvar_95": "Negative mean return at or below the fifth return percentile, expressed as a loss.",
    "sharpe": "Annualized arithmetic mean daily return divided by annualized volatility. Risk-free rate zero.",
    "sortino": "Annualized arithmetic mean daily return divided by annualized downside deviation. Target return zero.",
    "price_to_sma200": "Latest close / SMA200 - 1.",
    "sma200_slope": "Current SMA200 / SMA200 of the preceding 200-observation block - 1.",
    "golden_cross": "1 when SMA50 exceeds SMA200, 0 otherwise. This measures alignment.",
    "bollinger_upper": "SMA20 plus twice the trailing 20-close sample standard deviation.",
    "bollinger_lower": "SMA20 less twice the trailing 20-close sample standard deviation.",
    "atr14": "Mean of the last 14 true ranges. True range is max(high - low, |high - prior close|, |low - prior close|).",
    "adx14": "Mean of the last 14 directional index values, computed from 14-observation sums of directional moves and true ranges.",
    "relative_volume20": "Latest volume divided by the average volume of the last 20 observed sessions.",
    "distance_from_52w_high": "Latest close / highest intraday high over the trailing 252 observations - 1.",
    "rsi": "100 - 100 / (1 + mean up move / mean down move), using 14 simple observations.",
    "macd": "EMA12 less EMA26, seeded with the first observed close.",
    "momentum_12_minus_1": "Close 21 observations ago divided by close 252 observations ago - 1.",
    "relative_momentum_12m": "Asset 252-session price return less SPY price return over the same endpoints.",
    **{f"ma{n}": f"Arithmetic mean of the last {n} observed closes." for n in (20, 50, 100, 200)},
    **{
        f"ema{n}": f"Recursive exponential mean with smoothing factor 2/({n}+1), seeded on the first observed close."
        for n in (20, 50, 200)
    },
    **{
        f"return_{m}m": f"Latest close divided by the close {n} observations earlier, less one."
        for m, n in ((1, 21), (3, 63), (6, 126), (12, 252))
    },
}


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

        ma20 = float(closes.tail(20).mean()) if trading_days >= 20 else None
        ma50 = float(closes.tail(50).mean()) if trading_days >= 50 else None
        ma100 = float(closes.tail(100).mean()) if trading_days >= 100 else None
        ma200 = float(closes.tail(200).mean()) if trading_days >= 200 else None
        ema20 = float(closes.ewm_mean(span=20, adjust=False)[-1])
        ema50 = float(closes.ewm_mean(span=50, adjust=False)[-1])
        ema200 = float(closes.ewm_mean(span=200, adjust=False)[-1])
        bb_mid = ma20
        bb_std = float(closes.tail(20).std(ddof=1)) if trading_days >= 20 else None
        tr = _true_ranges(frame)
        atr14 = sum(tr[-14:]) / 14 if len(tr) >= 14 else None
        adx14 = _adx(frame)
        volume = frame["volume"] if "volume" in frame.columns else None
        relative_volume = (
            float(volume[-1]) / float(volume.tail(20).mean())
            if volume is not None and trading_days >= 20 and float(volume.tail(20).mean()) > 0
            else None
        )
        high52 = (
            max(_as_list(frame["high"].tail(252)))
            if "high" in frame.columns and trading_days >= 252
            else None
        )
        price_to_sma200 = latest / ma200 - 1 if ma200 else None
        sma200_change = sma200_slope(closes)
        golden_cross = (
            bool(ma50 and ma200 and ma50 > ma200)
            if ma50 is not None and ma200 is not None
            else None
        )
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
            "ma20": ma20,
            "ma50": ma50,
            "ma100": ma100,
            "ma200": ma200,
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "price_to_sma200": price_to_sma200,
            "sma200_slope": sma200_change,
            "golden_cross": golden_cross,
            "bollinger_upper": bb_mid + 2 * bb_std
            if bb_mid is not None and bb_std is not None
            else None,
            "bollinger_lower": bb_mid - 2 * bb_std
            if bb_mid is not None and bb_std is not None
            else None,
            "atr14": atr14,
            "adx14": adx14,
            "relative_volume20": relative_volume,
            "distance_from_52w_high": latest / high52 - 1 if high52 else None,
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


def sma200_slope(closes: pl.Series) -> float | None:
    if len(closes) < 400:
        return None
    return float(closes[-200:].mean()) / float(closes[-400:-200].mean()) - 1


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


def _as_list(series):
    return [float(value) for value in series.to_list()]


def _true_ranges(frame):
    if not {"high", "low"}.issubset(frame.columns):
        return []
    highs, lows, closes = map(_as_list, (frame["high"], frame["low"], frame["close"]))
    return [
        max(high - lows[i], abs(high - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i, high in enumerate(highs)
        if i > 0
    ]


def _adx(frame, window=14):
    if not {"high", "low", "close"}.issubset(frame.columns) or frame.height < 2 * window + 1:
        return None
    highs, lows, closes = map(_as_list, (frame["high"], frame["low"], frame["close"]))
    plus, minus, ranges = [], [], []
    for i in range(1, len(closes)):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus.append(up if up > down and up > 0 else 0)
        minus.append(down if down > up and down > 0 else 0)
        ranges.append(
            max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        )
    dx = []
    for i in range(window - 1, len(ranges)):
        atr = sum(ranges[i - window + 1 : i + 1])
        p = 100 * sum(plus[i - window + 1 : i + 1]) / atr if atr else 0
        m = 100 * sum(minus[i - window + 1 : i + 1]) / atr if atr else 0
        dx.append(100 * abs(p - m) / (p + m) if p + m else 0)
    return sum(dx[-window:]) / window if len(dx) >= window else None
