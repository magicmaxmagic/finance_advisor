"""Versioned personal records in SQLite, separate from the financial warehouse."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

Currency = Literal["USD", "CAD"]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]


class Trade(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    ticker: str = Field(pattern=r"^[A-Z0-9.:-]{1,64}$")
    date: date
    side: Literal["buy", "sell"]
    quantity: Positive
    price: Positive
    currency: Currency
    fee: float = Field(ge=0, allow_inf_nan=False, strict=True)
    rate: Positive


class Ledger(BaseModel):
    base: Currency = "USD"
    trades: list[Trade] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def validate_trades(self):
        if len({trade.id for trade in self.trades}) != len(self.trades):
            raise ValueError("Transaction IDs must be unique")
        if any(trade.date < date(1900, 1, 1) or trade.date > date.today() for trade in self.trades):
            raise ValueError("Transaction date is outside the supported time range")
        if any(trade.currency == self.base and trade.rate != 1 for trade in self.trades):
            raise ValueError("The exchange rate for base-currency transactions must be one")
        return self


class LedgerRevision(BaseModel):
    revision: int = Field(ge=0)
    updated_at: datetime | None = None
    ledger: Ledger


class LedgerUpdate(BaseModel):
    expected_revision: int = Field(ge=0)
    ledger: Ledger


class LedgerConflict(ValueError):
    pass


def _from_row(row: tuple | None) -> LedgerRevision:
    if not row:
        return LedgerRevision(revision=0, ledger=Ledger())
    rev, stamp, content = row
    return LedgerRevision(
        revision=rev, updated_at=stamp, ledger=Ledger.model_validate_json(content)
    )


def read_ledger(path: Path, revision: int | None = None) -> LedgerRevision | None:
    if not path.is_file():
        return _from_row(None) if revision is None or revision == 0 else None
    with closing(sqlite3.connect(path, timeout=10)) as con:
        table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_revision'"
        ).fetchone()
        if not table:
            return _from_row(None) if revision is None or revision == 0 else None
        if revision is None:
            row = con.execute(
                "SELECT revision,created_at,body FROM ledger_revision ORDER BY revision DESC LIMIT 1"
            ).fetchone()
        else:
            row = con.execute(
                "SELECT revision,created_at,body FROM ledger_revision WHERE revision=?", [revision]
            ).fetchone()
            if not row and revision != 0:
                return None
    return _from_row(row)


def save_ledger(path: Path, update: LedgerUpdate) -> LedgerRevision:
    """Store an entire validated revision in one atomic, compare-and-swap write."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(path, timeout=10)) as con, con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("""CREATE TABLE IF NOT EXISTS ledger_revision (
            revision INTEGER PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL)""")
        row = con.execute(
            "SELECT revision,created_at,body FROM ledger_revision ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        existing = _from_row(row)
        if existing.revision != update.expected_revision:
            raise LedgerConflict("This portfolio changed in another tab. Reload before editing.")
        if existing.revision and existing.ledger == update.ledger:
            return existing
        stamp = datetime.now(UTC)
        revision = existing.revision + 1
        con.execute(
            "INSERT INTO ledger_revision VALUES(?,?,?)",
            [revision, stamp.isoformat(), update.ledger.model_dump_json()],
        )
        return LedgerRevision(revision=revision, updated_at=stamp, ledger=update.ledger)
