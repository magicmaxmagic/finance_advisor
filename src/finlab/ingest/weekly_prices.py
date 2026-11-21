"""Read-only daily USD prices for the weekly report. Adjusted series stay out of price_daily."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from finlab.research.weekly import Bar, Bars

ENDPOINT = "https://data.alpaca.markets/v2/stocks/bars"
COLUMNS = ("ticker", "date", "raw_close", "split_close", "adjusted_close")


def read_csv(path: Path, week: date) -> Bars:
    bars: Bars = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not set(COLUMNS).issubset(reader.fieldnames or []):
            raise ValueError(f"Required price columns: {', '.join(COLUMNS)}")
        for row in reader:
            day = date.fromisoformat(row["date"])
            if day > week:
                continue
            ticker = row["ticker"].strip().upper()
            series = bars.setdefault(ticker, {})
            if not ticker or day in series:
                raise ValueError(f"Empty ticker or repeated price for {ticker} on {day}.")
            series[day] = Bar(*(float(row[col]) for col in COLUMNS[2:]))
    return bars


def download(client: httpx.Client, symbols: list[str], week: date) -> Bars:
    """Fetch fully adjusted returns, raw quotes and split factors for share-basis checks."""
    values: dict[tuple[str, date], dict[str, float]] = {}
    for adjustment in ("raw", "split", "all"):
        params = {
            "symbols": ",".join(sorted(set(symbols))),
            "timeframe": "1Day",
            "start": (week - timedelta(weeks=261)).isoformat(),
            "end": (week + timedelta(days=1)).isoformat(),
            "adjustment": adjustment,
            "asof": week.isoformat(),
            "feed": "iex",
            "currency": "USD",
            "limit": "10000",
            "sort": "asc",
        }
        seen = set()
        while True:
            response = client.get(ENDPOINT, params=params)
            response.raise_for_status()
            data = response.json()
            for symbol, records in (data["bars"] or {}).items():
                for record in records:
                    day = datetime.fromisoformat(record["t"].replace("Z", "+00:00")).date()
                    if day > week:
                        continue
                    record_values = values.setdefault((symbol, day), {})
                    if adjustment in record_values:
                        raise ValueError(f"Duplicate {adjustment} close for {symbol} on {day}.")
                    record_values[adjustment] = float(record["c"])
            token = data.get("next_page_token")
            if not token:
                break
            if token in seen:
                raise ValueError("Price service repeated the pagination token.")
            seen.add(token)
            params["page_token"] = token
    bars: Bars = {}
    for (symbol, day), entry in values.items():
        if len(entry) == 3:
            bars.setdefault(symbol, {})[day] = Bar(entry["raw"], entry["split"], entry["all"])
    return bars
