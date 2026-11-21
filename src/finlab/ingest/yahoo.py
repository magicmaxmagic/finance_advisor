"""Free Yahoo Finance provider adapter using yfinance."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol

import polars as pl
import yfinance as yf

from finlab.ingest.providers import DAILY_PRICE_SCHEMA


class YahooDownload(Protocol):
    def history(self, *args: Any, **kwargs: Any) -> Any: ...


class YahooProvider:
    name = "yahoo"

    def __init__(self, *, auto_adjust: bool = False, timeout: float = 30.0) -> None:
        self.auto_adjust = auto_adjust
        self.timeout = timeout

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
            actions=False,
            progress=False,
            threads=True,
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
            for timestamp, record in symbol_frame.iterrows():
                observed = (
                    timestamp.date()
                    if isinstance(timestamp, datetime)
                    else date.fromisoformat(str(timestamp)[:10])
                )
                try:
                    values = tuple(float(record[name]) for name in ("Open", "High", "Low", "Close"))
                    if not all(value > 0 for value in values):
                        continue
                    opened, high, low, closed = values
                    if high < max(opened, closed) or low > min(opened, closed):
                        continue
                    volume = int(record.get("Volume", 0) or 0)
                    if volume < 0:
                        continue
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
                        }
                    )
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue

        if not rows:
            return pl.DataFrame(schema=DAILY_PRICE_SCHEMA)
        normalized = pl.DataFrame(rows, schema={**DAILY_PRICE_SCHEMA, "symbol": pl.Utf8})
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
        actions: list[dict[str, Any]] = []
        for symbol in symbols:
            ticker = yf.Ticker(symbol)
            frame = ticker.actions
            if frame is None or getattr(frame, "empty", True):
                continue
            for timestamp, row in frame.iterrows():
                ex_date = (
                    timestamp.date()
                    if isinstance(timestamp, datetime)
                    else date.fromisoformat(str(timestamp)[:10])
                )
                if ex_date < start or ex_date > end:
                    continue
                dividend = float(row.get("Dividends", 0) or 0)
                split = float(row.get("Stock Splits", 0) or 0)
                if dividend:
                    actions.append(
                        {
                            "symbol": symbol,
                            "ex_date": ex_date,
                            "action_type": "cash_dividend",
                            "amount": dividend,
                            "source": self.name,
                        }
                    )
                if split:
                    actions.append(
                        {
                            "symbol": symbol,
                            "ex_date": ex_date,
                            "action_type": "split",
                            "factor": split,
                            "source": self.name,
                        }
                    )
        return actions
