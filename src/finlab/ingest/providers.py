"""Common market-data provider protocol and normalized Polars rows."""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

import polars as pl

DAILY_PRICE_SCHEMA = {
    "security_id": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
    "source": pl.Utf8,
}


class MarketDataProvider(Protocol):
    """A provider returns normalized, provider-tagged market observations."""

    name: str

    def get_daily_prices(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> pl.DataFrame:
        """Return normalized daily OHLCV, indexed by provider symbol and date."""

    def get_latest_quotes(self, symbols: list[str]) -> pl.DataFrame:
        """Return provider-symbol latest quotes, when available."""

    def get_corporate_actions(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        """Return normalized split/dividend/spinoff records."""
