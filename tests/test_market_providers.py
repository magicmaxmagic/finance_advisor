"""Offline tests for normalized market-data providers."""

from __future__ import annotations

from datetime import date

import polars as pl

from finlab.db.conn import write_conn
from finlab.ingest.providers import DAILY_PRICE_SCHEMA
from finlab.ingest.universe import Company
from finlab.ingest.yahoo_loader import load_yahoo_prices


def test_daily_price_schema_is_explicit():
    frame = pl.DataFrame(schema=DAILY_PRICE_SCHEMA)
    assert frame.schema == DAILY_PRICE_SCHEMA


def test_corporate_action_projection():
    actions = [
        {
            "symbol": "AAPL",
            "ex_date": date(2024, 8, 9),
            "action_type": "cash_dividend",
            "amount": 0.25,
            "source": "yahoo",
        },
        {
            "symbol": "AAPL",
            "ex_date": date(2020, 8, 31),
            "action_type": "split",
            "factor": 4.0,
            "source": "yahoo",
        },
    ]
    assert actions[0]["action_type"] == "cash_dividend"
    assert actions[1]["factor"] == 4.0


def test_mock_provider_contract_shape():
    actions = {"AAPL": ["split", "cash_dividend"]}
    assert actions["AAPL"] == ["split", "cash_dividend"]


def test_yahoo_loader_contract_is_normalized():
    normalized = pl.DataFrame(
        [
            {
                "symbol": "AAPL",
                "date": date(2024, 1, 2),
                "open": 185.0,
                "high": 188.0,
                "low": 184.0,
                "close": 187.0,
                "volume": 1000,
                "source": "yahoo",
            }
        ],
        schema={**DAILY_PRICE_SCHEMA, "symbol": pl.Utf8},
    )
    assert normalized.height == 1
    assert normalized[0, "source"] == "yahoo"


def test_yahoo_loader_publishes_prices_and_actions(initialised, tmp_path):
    class MockYahoo:
        name = "yahoo"

        def get_daily_prices(self, symbols, start, end):
            return pl.DataFrame(
                [
                    {
                        "symbol": "AAA",
                        "date": date(2024, 1, 2),
                        "open": 100.0,
                        "high": 102.0,
                        "low": 99.0,
                        "close": 101.0,
                        "volume": 500,
                        "source": "yahoo",
                    }
                ],
                schema={**DAILY_PRICE_SCHEMA, "symbol": pl.Utf8},
            )

        def get_corporate_actions(self, symbols, start, end):
            return [
                {
                    "symbol": "AAA",
                    "ex_date": date(2024, 6, 1),
                    "action_type": "cash_dividend",
                    "amount": 0.25,
                    "source": "yahoo",
                }
            ]

    company = Company(1, "AAA", "Technology", "Alpha")
    with write_conn(initialised) as con:
        report = load_yahoo_prices(
            con,
            initialised,
            MockYahoo(),
            companies=(company,),
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
        )

    assert report.rows == 1
    assert report.tickers_found == ["AAA"]
    assert report.corporate_actions == 1
    assert (initialised.warehouse_dir / "price_daily").is_dir()


def test_explicit_stable_identity_survives_ticker_change(initialised):
    from finlab.ingest.prices import build_securities

    old = Company(1, "OLD", "Technology", "Issuer", stable_id="US:stable-line")
    new = Company(1, "NEW", "Technology", "Issuer", stable_id="US:stable-line")
    with write_conn(initialised) as con:
        build_securities(con, initialised, (old,))
        con.execute(
            "UPDATE security SET exchange='NASDAQ',first_date=DATE '2000-01-01' WHERE security_id='US:stable-line'"
        )
        build_securities(con, initialised, (new,))
        assert con.execute(
            "SELECT ticker,exchange FROM security WHERE security_id='US:stable-line'"
        ).fetchone() == ("NEW", "NASDAQ")
        assert (
            con.execute("SELECT count(*) FROM ticker_history WHERE ticker='OLD'").fetchone()[0] == 1
        )
        assert con.execute(
            "SELECT ticker FROM ticker_history WHERE valid_to=DATE '9999-12-31'"
        ).fetchone() == ("NEW",)


def test_yahoo_prices_and_actions_share_one_bounded_download(monkeypatch):
    import pandas as pd

    from finlab.ingest.yahoo import YahooProvider

    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame(
            {
                "Open": [100.0, 50.0],
                "High": [101.0, 51.0],
                "Low": [99.0, 49.0],
                "Close": [100.0, 50.0],
                "Adj Close": [49.0, 50.0],
                "Volume": [100, 200],
                "Dividends": [1.0, 0.0],
                "Stock Splits": [0.0, 2.0],
            },
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        )

    monkeypatch.setattr("finlab.ingest.yahoo.yf.download", download)
    provider = YahooProvider()
    frame = provider.get_daily_prices(["AAA"], date(2024, 1, 2), date(2024, 1, 3))
    actions = provider.get_corporate_actions(["AAA"], date(2024, 1, 2), date(2024, 1, 3))
    assert len(calls) == 1
    assert calls[0]["actions"] and calls[0]["threads"] <= 4
    assert frame["adjusted_close"].to_list() == [49.0, 50.0]
    assert {action["action_type"] for action in actions} == {"split", "cash_dividend"}
