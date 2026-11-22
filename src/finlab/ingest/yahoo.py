"""Free Yahoo Finance provider adapter using yfinance."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Any, Protocol

import polars as pl
import yfinance as yf

from finlab.ingest.providers import DAILY_PRICE_SCHEMA


class YahooDownload(Protocol):
    def history(self, *args: Any, **kwargs: Any) -> Any: ...


class YahooProvider:
    name = "yahoo"

    def __init__(self, *, auto_adjust: bool = False, timeout: float = 30.0) -> None:
        if auto_adjust:
            raise ValueError("Adjusted OHLC cannot be loaded into the raw Yahoo price contract")
        self.auto_adjust = False
        self.timeout = timeout
        self._actions_cache: dict[tuple[str, date, date], list[dict[str, Any]]] = {}

    def get_daily_prices(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame(schema=DAILY_PRICE_SCHEMA)

        frame = yf.download(
            tickers=symbols,
            start=start.isoformat(),
            end=(end.fromordinal(end.toordinal() + 1)).isoformat(),
            interval="1d",
            auto_adjust=self.auto_adjust,
            actions=True,
            progress=False,
            threads=min(4, len(symbols)),
            timeout=self.timeout,
            group_by="ticker",
        )
        if frame is None or frame.empty:
            return pl.DataFrame(schema=DAILY_PRICE_SCHEMA)

        rows: list[dict[str, Any]] = []
        multi = getattr(frame.columns, "nlevels", 1) > 1
        for symbol in symbols:
            if multi:
                try:
                    symbol_frame = frame[symbol]
                except (KeyError, TypeError):
                    continue
            else:
                symbol_frame = frame
            symbol_actions = []
            before_rows = len(rows)
            for timestamp, record in symbol_frame.iterrows():
                observed = (
                    timestamp.date()
                    if isinstance(timestamp, datetime)
                    else date.fromisoformat(str(timestamp)[:10])
                )
                try:
                    values = tuple(float(record[name]) for name in ("Open", "High", "Low", "Close"))
                    if not all(math.isfinite(value) and value > 0 for value in values):
                        continue
                    opened, high, low, closed = values
                    if high < max(opened, closed) or low > min(opened, closed):
                        continue
                    volume = int(record.get("Volume", 0) or 0)
                    if volume < 0:
                        continue
                    if "Dividends" not in record or "Stock Splits" not in record:
                        raise RuntimeError(f"Yahoo action fields missing for {symbol}")
                    dividend = float(record.get("Dividends", 0) or 0)
                    split = float(record.get("Stock Splits", 0) or 0)
                    if (
                        not math.isfinite(dividend)
                        or not math.isfinite(split)
                        or dividend < 0
                        or split < 0
                    ):
                        raise RuntimeError(f"Invalid Yahoo action observation for {symbol}")
                    if dividend and math.isfinite(dividend):
                        symbol_actions.append(
                            dict(
                                symbol=symbol,
                                ex_date=observed,
                                action_type="cash_dividend",
                                amount=dividend,
                                source=self.name,
                            )
                        )
                    if split and math.isfinite(split):
                        symbol_actions.append(
                            dict(
                                symbol=symbol,
                                ex_date=observed,
                                action_type="split",
                                factor=split,
                                source=self.name,
                            )
                        )
                    rows.append(
                        {
                            "symbol": symbol,
                            "date": observed,
                            "open": opened,
                            "high": high,
                            "low": low,
                            "close": closed,
                            "volume": volume,
                            "source": self.name,
                            "adjusted_close": float(record["Adj Close"])
                            if "Adj Close" in record
                            and math.isfinite(float(record["Adj Close"]))
                            and float(record["Adj Close"]) > 0
                            else None,
                            "adjustment_basis": "yahoo_auto_adjust_false",
                            "ingested_at": datetime.now(UTC),
                        }
                    )
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
            if len(rows) > before_rows:
                self._actions_cache[(symbol, start, end)] = symbol_actions

        if not rows:
            return pl.DataFrame(schema=DAILY_PRICE_SCHEMA)
        normalized = pl.DataFrame(
            rows,
            schema={
                **DAILY_PRICE_SCHEMA,
                "symbol": pl.Utf8,
                "adjusted_close": pl.Float64,
                "adjustment_basis": pl.Utf8,
                "ingested_at": pl.Datetime("us", "UTC"),
            },
        )
        return normalized

    def get_latest_quotes(self, symbols: list[str]) -> pl.DataFrame:
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            ticker = yf.Ticker(symbol)
            fast = getattr(ticker, "fast_info", {})
            try:
                price = float(fast.get("last_price") or fast.get("regular_market_price"))
                currency = str(fast.get("currency") or "USD")
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            rows.append(
                {"symbol": symbol, "price": price, "currency": currency, "source": self.name}
            )
        return pl.DataFrame(
            rows,
            schema={"symbol": pl.Utf8, "price": pl.Float64, "currency": pl.Utf8, "source": pl.Utf8},
        )

    def get_corporate_actions(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        missing = [symbol for symbol in symbols if (symbol, start, end) not in self._actions_cache]
        if missing:
            self.get_daily_prices(missing, start, end)
        if any((symbol, start, end) not in self._actions_cache for symbol in symbols):
            raise RuntimeError("Yahoo corporate-action coverage is incomplete")
        return [
            action for symbol in symbols for action in self._actions_cache[(symbol, start, end)]
        ]
