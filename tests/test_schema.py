"""The schema matches docs/architecture.md.

The table list is written out longhand on purpose: it is a restatement of the
design document, so a table quietly renamed or dropped in `schema.sql` fails
here rather than surfacing as a confusing query error three milestones later.
"""

from __future__ import annotations

import pytest

from finlab.config import Settings
from finlab.db.conn import open_db_read_only
from finlab.db.keys import fact_key_sql

# docs/architecture.md §3-§8, plus the migration runner's own watermark table.
EXPECTED_TABLES = frozenset(
    {
        # meta
        "schema_version",
        # L1 core
        "entity",
        "security",
        "ticker_history",
        "filing",
        "fact",
        "price_daily",
        "price_ingestion",
        "corporate_action",
        # L2 canonical
        "concept_map",
        "universe_snapshot",
        # L3 derived
        "metric",
        "valuation_run",
        # L4 text
        "document",
        "document_section",
        "extraction",
        # L5 output
        "report",
        "claim",
        # research
        "signal",
        "signal_value",
        "research_trial",
        "backtest_run",
        "backtest_metric",
        # orchestration
        "job",
    }
)


def _table_names(con) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }


def test_every_documented_table_exists(initialised: Settings) -> None:
    with open_db_read_only(initialised) as con:
        assert _table_names(con) == EXPECTED_TABLES


def test_all_tables_start_empty(initialised: Settings) -> None:
    with open_db_read_only(initialised) as con:
        for name in sorted(EXPECTED_TABLES - {"schema_version"}):
            count = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            assert count == 0, f"{name} is not empty on a fresh database"


_FACT_COLUMNS = (
    "cik, taxonomy, tag, unit, period_start, period_end, duration_days, "
    "duration_bucket, value, accession_number, filed_date"
)


def _insert_fact_sql() -> str:
    """Insert into `fact` from a source subquery, deriving fact_key in SQL.

    The same shape the ingester writes with: the key is never computed in
    Python.
    """
    return (
        f"INSERT INTO fact (fact_key, {_FACT_COLUMNS}) "
        f"SELECT {fact_key_sql()}, {_FACT_COLUMNS} FROM source"
    )


def test_fact_accepts_instant_facts_with_null_period_start(initialised: Settings) -> None:
    """Balance-sheet facts have no period_start.

    The architecture document lists period_start in the fact primary key, but
    DuckDB forces primary-key columns NOT NULL, which would reject every
    instant. schema.sql documents the logical key instead of declaring it.
    """
    from finlab.db.conn import write_conn

    with write_conn(initialised) as con:
        con.execute(
            f"""
            WITH source AS (
              SELECT 320193 AS cik, 'us-gaap' AS taxonomy, 'Assets' AS tag, 'USD' AS unit,
                     NULL::DATE AS period_start, DATE '2024-09-28' AS period_end,
                     NULL::INTEGER AS duration_days, 'instant' AS duration_bucket,
                     364980000000.0 AS value, '0000320193-24-000123' AS accession_number,
                     DATE '2024-11-01' AS filed_date
            )
            {_insert_fact_sql()}
            """
        )
        assert con.execute("SELECT count(*) FROM fact").fetchone()[0] == 1
        assert con.execute("SELECT fact_key IS NOT NULL FROM fact").fetchone()[0]


def test_fact_preserves_restatements(initialised: Settings) -> None:
    """The same period reported in two filings must produce two rows.

    That redundancy is the restatement history; a physical primary key that
    collapsed these would destroy the point-in-time property. The two rows must
    also get distinct fact_keys, since accession_number is part of the key.
    """
    from finlab.db.conn import write_conn

    rows = [
        ("0000320193-24-000100", "2024-11-01", 391_035_000_000.0),
        ("0000320193-25-000073", "2025-02-01", 391_100_000_000.0),
    ]
    with write_conn(initialised) as con:
        for accession, filed, value in rows:
            con.execute(
                f"""
                WITH source AS (
                  SELECT 320193 AS cik, 'us-gaap' AS taxonomy, 'Revenues' AS tag,
                         'USD' AS unit, DATE '2023-10-01' AS period_start,
                         DATE '2024-09-28' AS period_end, 363 AS duration_days,
                         'annual' AS duration_bucket, ? AS value,
                         ? AS accession_number, ?::DATE AS filed_date
                )
                {_insert_fact_sql()}
                """,
                [value, accession, filed],
            )
        distinct_filed, distinct_keys = con.execute(
            "SELECT count(DISTINCT filed_date), count(DISTINCT fact_key) "
            "FROM fact WHERE tag = 'Revenues'"
        ).fetchone()
    assert distinct_filed == 2
    assert distinct_keys == 2


@pytest.mark.parametrize("index_name", ["filing_cik_filed", "job_claim"])
def test_indexes_exist(initialised: Settings, index_name: str) -> None:
    with open_db_read_only(initialised) as con:
        names = {
            row[0] for row in con.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
        }
    assert index_name in names
