"""Deterministic recovery tests; no external providers are contacted."""

from datetime import date

import polars as pl
import pytest

from finlab.db.conn import write_conn
from finlab.ingest.price_sync import IncompletePriceSync, plan_price_sync, sync_prices
from finlab.ingest.providers import DAILY_PRICE_SCHEMA
from finlab.ingest.universe import Company

AAA = Company(1, "AAA", "Technology", "Alpha")
BBB = Company(2, "BBB", "Technology", "Beta")


def test_outage_and_new_security_have_independent_recovery_windows():
    windows = plan_price_sync(
        (AAA, BBB),
        {AAA.security_id: date(2024, 1, 31)},
        history_start=date(2006, 1, 1),
        end=date(2024, 6, 1),
        overlap_days=10,
    )
    assert [(w.start, w.companies) for w in windows] == [
        (date(2006, 1, 1), (BBB,)),
        (date(2024, 1, 21), (AAA,)),
    ]


def test_same_windows_are_grouped_and_history_start_is_respected():
    windows = plan_price_sync(
        (BBB, AAA),
        {AAA.security_id: date(2024, 1, 2)},
        history_start=date(2024, 1, 1),
        end=date(2024, 6, 1),
        overlap_days=10,
    )
    assert len(windows) == 1
    assert windows[0].companies == (AAA, BBB)
    assert windows[0].start == date(2024, 1, 1)


class FixtureProvider:
    def __init__(self):
        self.requests = []
        self.missing = set()
        self.actions_fail = False
        self.history = {}

    def get_daily_prices(self, symbols, start, end):
        self.requests.append((tuple(symbols), start, end))
        for symbol in symbols:
            if symbol not in self.missing:
                self.history.setdefault(symbol, set()).add(end)
        rows = [
            dict(
                symbol=symbol,
                date=day,
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                volume=100,
                source="yahoo",
            )
            for symbol in symbols
            if symbol not in self.missing
            for day in sorted(self.history[symbol])
            if start <= day <= end
        ]
        return pl.DataFrame(rows, schema={**DAILY_PRICE_SCHEMA, "symbol": pl.Utf8})

    def get_corporate_actions(self, symbols, start, end):
        if self.actions_fail:
            raise RuntimeError("Action provider unavailable")
        return []


def test_persisted_watermark_recovers_long_outage_and_retry_is_idempotent(initialised):
    provider = FixtureProvider()
    with write_conn(initialised) as con:
        sync_prices(con, initialised, provider, end=date(2024, 1, 31), companies=(AAA,))
        sync_prices(con, initialised, provider, end=date(2024, 6, 1), companies=(AAA,))
        assert provider.requests[-1][1] == date(2024, 1, 21)
        sync_prices(con, initialised, provider, end=date(2024, 6, 1), companies=(AAA,))
        assert provider.requests[-1][1] == date(2024, 5, 22)
        count = con.execute(
            "SELECT count(*) FROM read_parquet(?)",
            [str(initialised.warehouse_dir / "price_daily/**/*.parquet")],
        ).fetchone()[0]
        assert count == 2
    assert not (initialised.warehouse_dir / "snapshots/latest.json").exists()


def test_missing_symbol_retries_without_losing_successful_symbols(initialised):
    provider = FixtureProvider()
    provider.missing = {"BBB"}
    with write_conn(initialised) as con:
        with pytest.raises(IncompletePriceSync, match="BBB"):
            sync_prices(con, initialised, provider, end=date(2024, 1, 31), companies=(AAA, BBB))
        provider.missing.clear()
        sync_prices(con, initialised, provider, end=date(2024, 2, 1), companies=(AAA, BBB))
    assert (("BBB",), date(2006, 1, 1), date(2024, 2, 1)) in provider.requests
    assert (("AAA",), date(2024, 1, 21), date(2024, 2, 1)) in provider.requests


def test_action_download_failure_does_not_advance_price_watermark(initialised):
    provider = FixtureProvider()
    provider.actions_fail = True
    with write_conn(initialised) as con:
        with pytest.raises(RuntimeError, match="Action provider"):
            sync_prices(con, initialised, provider, end=date(2024, 1, 31), companies=(AAA,))
    assert not list((initialised.warehouse_dir / "price_daily").rglob("*.parquet"))


def test_adjustment_revision_refreshes_only_the_affected_security(initialised):
    class RevisedProvider(FixtureProvider):
        name = "yahoo"
        revision = False

        def get_daily_prices(self, symbols, start, end):
            self.requests.append((tuple(symbols), start, end))
            days = [day for day in (date(2024, 1, 1), date(2024, 2, 1)) if start <= day <= end]
            return pl.DataFrame(
                [
                    dict(
                        symbol=s,
                        date=d,
                        open=100.0,
                        high=100.0,
                        low=100.0,
                        close=100.0,
                        volume=100,
                        source="yahoo",
                        adjusted_close=99.0 if self.revision else 90.0,
                        adjustment_basis="yahoo_auto_adjust_false",
                    )
                    for s in symbols
                    for d in days
                ]
            )

    from finlab.ingest.yahoo_loader import load_yahoo_prices

    provider = RevisedProvider()
    with write_conn(initialised) as con:
        load_yahoo_prices(
            con, initialised, provider, (AAA,), start=date(2024, 1, 1), end=date(2024, 2, 1)
        )
        provider.revision = True
        load_yahoo_prices(
            con, initialised, provider, (AAA,), start=date(2024, 1, 25), end=date(2024, 2, 1)
        )
        assert provider.requests[-1][1] == date(2024, 1, 1)
        values = con.execute(
            "SELECT adjusted_close FROM read_parquet(?) ORDER BY date",
            [str(initialised.warehouse_dir / "price_daily/**/*.parquet")],
        ).fetchall()
        assert values == [(99.0,), (99.0,)]
