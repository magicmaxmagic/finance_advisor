"""Independent per-symbol Parquet history cache; quotes never invalidate history."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

import polars as pl

from finlab.config import Settings
from finlab.ingest.live_market import history
from finlab.research.weekly import Bar, Bars


def cached_history(settings: Settings, symbols: tuple[str, ...], cutoff: date) -> Bars:
    """Batch only missing symbols; adding one ticker never reloads its neighbours."""
    directory = settings.data_dir / "cache" / "live-prices" / cutoff.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    bars: Bars = {}
    missing = []
    for symbol in symbols:
        path = directory / f"{symbol}.parquet"
        if not path.is_file():
            missing.append(symbol)
            continue
        frame = pl.read_parquet(path)
        bars[symbol] = {
            row["date"]: Bar(row["raw"], row["split"], row["adjusted"])
            for row in frame.iter_rows(named=True)
        }
    if missing:
        downloaded = history(settings, missing, cutoff)
        for symbol, series in downloaded.items():
            if not series:
                continue
            frame = pl.DataFrame(
                [
                    {"date": day, "raw": b.raw, "split": b.split, "adjusted": b.adjusted}
                    for day, b in sorted(series.items())
                ]
            )
            with NamedTemporaryFile(dir=directory, suffix=".parquet", delete=False) as handle:
                temporary = Path(handle.name)
            try:
                frame.write_parquet(temporary)
                os.replace(temporary, directory / f"{symbol}.parquet")
            finally:
                temporary.unlink(missing_ok=True)
            bars[symbol] = series
    return bars
