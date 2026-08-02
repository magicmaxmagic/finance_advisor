"""Nasdaq Trader symbol directory -> `universe_snapshot`, `security`.

The daily snapshot loader lands in M3. What exists today is the fixed
proof-of-concept universe: a hand-maintained list of large-cap filers, pinned by
CIK, used to exercise the whole pipeline on a small sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

POC_UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "data" / "poc_universe.yaml"


@dataclass(frozen=True, slots=True)
class Company:
    """One filer in the proof-of-concept universe."""

    cik: int
    ticker: str
    sector: str
    name: str

    @property
    def security_id(self) -> str:
        """The synthetic stable key from architecture §3.2.

        Built from the CIK, not the ticker, because tickers are recycled.
        """
        return f"US:{self.cik:010d}:{self.ticker}"


class UniverseError(RuntimeError):
    """The universe file is missing or malformed."""


@lru_cache(maxsize=4)
def load_poc_universe(path: Path | None = None) -> tuple[Company, ...]:
    """Load and validate the PoC universe, ordered by ticker."""
    target = path or POC_UNIVERSE_PATH
    if not target.is_file():
        raise UniverseError(f"PoC universe file not found: {target}")

    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    entries = document.get("companies") if isinstance(document, dict) else None
    if not entries:
        raise UniverseError(f"{target} declares no companies")

    companies: list[Company] = []
    seen_cik: dict[int, str] = {}
    for entry in entries:
        missing = {"cik", "ticker", "sector", "name"} - set(entry)
        if missing:
            raise UniverseError(f"{target}: entry {entry!r} is missing {sorted(missing)}")
        cik = int(entry["cik"])
        if cik in seen_cik:
            raise UniverseError(
                f"{target}: CIK {cik} appears twice ({seen_cik[cik]} and {entry['ticker']})"
            )
        seen_cik[cik] = str(entry["ticker"])
        companies.append(
            Company(
                cik=cik,
                ticker=str(entry["ticker"]),
                sector=str(entry["sector"]),
                name=str(entry["name"]),
            )
        )
    return tuple(sorted(companies, key=lambda c: c.ticker))


def sectors() -> tuple[str, ...]:
    return tuple(sorted({c.sector for c in load_poc_universe()}))
