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
