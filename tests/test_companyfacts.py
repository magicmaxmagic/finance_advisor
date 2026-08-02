"""The companyfacts parser and loader."""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from finlab.ingest.companyfacts import (
    FACT_COLUMNS,
    ParseStats,
    duration_bucket,
    parse_companyfacts,
)


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (None, "instant"),
        (79, "other"),
        (80, "quarter"),
        (90, "quarter"),
        (100, "quarter"),
        (101, "other"),
        (170, "half"),
        (190, "half"),
        (260, "ytd9"),
        (285, "ytd9"),
        (350, "annual"),
        (365, "annual"),
        (380, "annual"),
        (381, "other"),
    ],
)
def test_duration_buckets_follow_the_architecture(days: int | None, expected: str) -> None:
    """A 10-Q reports the same tag for 3 months and 9 months. Without these
    boundaries the two get summed together."""
    assert duration_bucket(days) == expected


def _payload(observations: list[dict]) -> dict:
    return {
        "cik": 320193,
        "entityName": "APPLE INC",
        "facts": {"us-gaap": {"Revenues": {"units": {"USD": observations}}}},
    }


def test_parse_emits_one_row_per_observation() -> None:
    stats = ParseStats()
    rows = list(
        parse_companyfacts(
            _payload(
                [
                    {
                        "start": "2023-10-01",
                        "end": "2024-09-28",
                        "val": 391_035_000_000,
                        "accn": "acc-1",
                        "fy": 2024,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2024-11-01",
                    }
                ]
            ),
            stats,
        )
    )
    assert len(rows) == 1
    row = dict(zip(FACT_COLUMNS, rows[0], strict=True))
    assert row["cik"] == 320193
    assert row["duration_days"] == 363
    assert row["duration_bucket"] == "annual"
    assert row["value"] == 391_035_000_000.0
    assert row["filed_date"] == date(2024, 11, 1)
    assert stats.emitted == 1


def test_restatements_are_preserved_not_collapsed() -> None:
    """The same period from two filings must survive as two rows."""
    base = {
        "start": "2023-10-01",
        "end": "2024-09-28",
        "val": 391_035_000_000,
        "fy": 2024,
        "fp": "FY",
        "form": "10-K",
    }
    rows = list(
        parse_companyfacts(
            _payload(
                [
                    {**base, "accn": "acc-1", "filed": "2024-11-01"},
                    {**base, "accn": "acc-2", "filed": "2025-11-01", "val": 391_100_000_000},
                ]
            )
        )
    )
    assert len(rows) == 2
    filed = {dict(zip(FACT_COLUMNS, r, strict=True))["filed_date"] for r in rows}
    assert filed == {date(2024, 11, 1), date(2025, 11, 1)}


def test_instant_facts_have_no_period_start() -> None:
    rows = list(
        parse_companyfacts(
            {
                "cik": 320193,
                "facts": {
                    "us-gaap": {
                        "Assets": {
                            "units": {
                                "USD": [
                                    {
                                        "end": "2024-09-28",
                                        "val": 364_980_000_000,
                                        "accn": "acc-1",
                                        "filed": "2024-11-01",
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        )
    )
    row = dict(zip(FACT_COLUMNS, rows[0], strict=True))
    assert row["period_start"] is None
    assert row["duration_days"] is None
    assert row["duration_bucket"] == "instant"


@pytest.mark.parametrize(
    ("observation", "counter"),
    [
        ({"end": "2024-09-28", "val": 1.0, "filed": "2024-11-01"}, "skipped_missing_field"),
        (
            {"end": "2024-09-28", "val": None, "accn": "a", "filed": "2024-11-01"},
            "skipped_null_value",
        ),
        (
            {"end": "not-a-date", "val": 1.0, "accn": "a", "filed": "2024-11-01"},
            "skipped_bad_date",
        ),
    ],
)
def test_malformed_observations_are_counted_not_silently_dropped(
    observation: dict, counter: str
) -> None:
    stats = ParseStats()
    assert list(parse_companyfacts(_payload([observation]), stats)) == []
    assert getattr(stats, counter) == 1
    assert stats.observations == 1


def test_every_taxonomy_is_parsed_not_just_us_gaap() -> None:
    payload = {
        "cik": 1,
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {"end": "2024-01-01", "val": 1.0, "accn": "a", "filed": "2024-02-01"}
                        ]
                    }
                }
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {"end": "2024-01-01", "val": 5.0, "accn": "a", "filed": "2024-02-01"}
                        ]
                    }
                }
            },
        },
    }
    taxonomies = {
        dict(zip(FACT_COLUMNS, r, strict=True))["taxonomy"] for r in parse_companyfacts(payload)
    }
    assert taxonomies == {"us-gaap", "dei"}


def test_conflicting_fact_keys_are_detected() -> None:
    """Two rows with the same logical key but different values is a parser bug.

    Restatements differ by accession_number, which is part of the key, so they
    never collide.
    """
    from finlab.db.keys import fact_key_sql

    con = duckdb.connect(":memory:")
    con.execute(
        f"""
        CREATE TABLE staged AS
        SELECT {fact_key_sql()} AS fact_key, * FROM (VALUES
          (1, 'us-gaap', 'Revenues', 'USD', DATE '2024-01-01', DATE '2024-12-31', 'acc-1', 10.0),
          (1, 'us-gaap', 'Revenues', 'USD', DATE '2024-01-01', DATE '2024-12-31', 'acc-1', 99.0),
          (1, 'us-gaap', 'Revenues', 'USD', DATE '2024-01-01', DATE '2024-12-31', 'acc-2', 10.0)
        ) t(cik, taxonomy, tag, unit, period_start, period_end, accession_number, value)
        """
    )
    conflicts = con.execute(
        "SELECT count(*) FROM (SELECT fact_key FROM staged GROUP BY fact_key "
        "HAVING count(DISTINCT value) > 1)"
    ).fetchone()[0]
    assert conflicts == 1, "the acc-1 pair conflicts; the acc-2 row is a legitimate restatement"
