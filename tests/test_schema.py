"""The schema matches docs/architecture.md.

The table list is written out longhand on purpose: it is a restatement of the
design document, so a table quietly renamed or dropped in `schema.sql` fails
here rather than surfacing as a confusing query error three milestones later.
"""

from __future__ import annotations

import pytest

from finlab.config import Settings
from finlab.db.conn import open_db_read_only

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


def test_fact_accepts_instant_facts_with_null_period_start(initialised: Settings) -> None:
    """Balance-sheet facts have no period_start.

    The architecture document lists period_start in the fact primary key, but
    DuckDB forces primary-key columns NOT NULL, which would reject every
    instant. schema.sql documents the logical key instead of declaring it.
    """
    from finlab.db.conn import write_conn

    with write_conn(initialised) as con:
        con.execute(
            """
            INSERT INTO fact (cik, taxonomy, tag, unit, period_start, period_end,
                              duration_days, duration_bucket, value,
                              accession_number, filed_date)
            VALUES (320193, 'us-gaap', 'Assets', 'USD', NULL, DATE '2024-09-28',
                    NULL, 'instant', 364980000000.0,
                    '0000320193-24-000123', DATE '2024-11-01')
            """
        )
        assert con.execute("SELECT count(*) FROM fact").fetchone()[0] == 1


def test_fact_preserves_restatements(initialised: Settings) -> None:
    """The same period reported in two filings must produce two rows.

    That redundancy is the restatement history; a physical primary key that
    collapsed these would destroy the point-in-time property.
    """
    from finlab.db.conn import write_conn

    rows = [
        ("0000320193-24-000100", "2024-11-01", 391_035_000_000.0),
        ("0000320193-25-000073", "2025-02-01", 391_100_000_000.0),
    ]
    with write_conn(initialised) as con:
        for accession, filed, value in rows:
            con.execute(
                """
                INSERT INTO fact (cik, taxonomy, tag, unit, period_start, period_end,
                                  duration_days, duration_bucket, value,
                                  accession_number, filed_date)
                VALUES (320193, 'us-gaap', 'Revenues', 'USD', DATE '2023-10-01',
                        DATE '2024-09-28', 363, 'annual', ?, ?, ?)
                """,
                [value, accession, filed],
            )
        distinct_filed = con.execute(
            "SELECT count(DISTINCT filed_date) FROM fact WHERE tag = 'Revenues'"
        ).fetchone()[0]
    assert distinct_filed == 2


@pytest.mark.parametrize("index_name", ["filing_cik_filed", "job_claim"])
def test_indexes_exist(initialised: Settings, index_name: str) -> None:
    with open_db_read_only(initialised) as con:
        names = {
            row[0] for row in con.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
        }
    assert index_name in names
