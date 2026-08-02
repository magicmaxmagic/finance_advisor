"""The read path must not contend with the writer.

CLAUDE.md invariant 2 says exactly one process holds the read-write DuckDB
connection. The UI is the case that matters in practice: it has to keep serving
while `finlabd` is mid-ingest. These tests hold the write lock in *this* process
and drive the app's real query layer from a *separate* one, which is the only
arrangement that can actually prove it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path

import duckdb
import pytest

from finlab.config import Settings
from finlab.db.conn import write_conn

# A miniature warehouse: one company, one concept, two filings of the same
# period so the point-in-time behaviour is observable.
_BUILD_WAREHOUSE = """
COPY (SELECT * FROM (VALUES
  (1, 320193, 'Revenues', 'USD', DATE '2023-10-01', DATE '2024-09-28', 363,
   'annual', 100.0, 'acc-1', '10-K', 2024, 'FY', DATE '2024-11-01', NULL),
  (2, 320193, 'Revenues', 'USD', DATE '2023-10-01', DATE '2024-09-28', 363,
   'annual', 175.0, 'acc-2', '10-K', 2024, 'FY', DATE '2025-11-01', NULL),
  -- Same filing, same period_end, different window: a 10-K tags both the year
  -- and the closing quarter.
  (3, 320193, 'Revenues', 'USD', DATE '2024-06-30', DATE '2024-09-28', 90,
   'quarter', 40.0, 'acc-1', '10-K', 2024, 'FY', DATE '2024-11-01', NULL)
) t(fact_key, cik, tag, unit, period_start, period_end, duration_days,
    duration_bucket, value, accession_number, form, fiscal_year, fiscal_period,
    filed_date, frame))
TO '{warehouse}/fact/taxonomy=us-gaap/filed_year=2024/part-0.parquet' (FORMAT PARQUET);

COPY (SELECT 320193 AS cik, 'APPLE INC' AS name, 'Information Technology' AS sector)
TO '{warehouse}/entity/part-0.parquet' (FORMAT PARQUET);

COPY (SELECT * FROM (VALUES
  ('acc-1', 320193, '10-K', DATE '2024-11-01'),
  ('acc-2', 320193, '10-K', DATE '2025-11-01')
) t(accession_number, cik, form, filed_date))
TO '{warehouse}/filing/part-0.parquet' (FORMAT PARQUET);

COPY (SELECT 'revenue' AS canonical, 'us-gaap' AS taxonomy, 'Revenues' AS tag,
             1 AS priority, 1::SMALLINT AS sign,
             DATE '1900-01-01' AS valid_from, DATE '9999-12-31' AS valid_to)
TO '{warehouse}/concept_map/part-0.parquet' (FORMAT PARQUET);
"""


@pytest.fixture
def warehouse(settings: Settings) -> Settings:
    settings.ensure_dirs()
    (settings.warehouse_dir / "fact" / "taxonomy=us-gaap" / "filed_year=2024").mkdir(parents=True)
    (settings.warehouse_dir / "entity").mkdir(parents=True)
    (settings.warehouse_dir / "filing").mkdir(parents=True)
    (settings.warehouse_dir / "concept_map").mkdir(parents=True)
    con = duckdb.connect(":memory:")
    con.execute(_BUILD_WAREHOUSE.format(warehouse=settings.warehouse_dir))
    con.close()
    return settings


def _run_reader(settings: Settings, body: str) -> subprocess.CompletedProcess[str]:
    """Run a snippet against the warehouse in a genuinely separate process."""
    script = "\n".join(
        [
            "from datetime import date",
            "from finlab.config import Settings",
            "from finlab.ui.queries import open_reader",
            f'settings = Settings(data_dir=r"{settings.data_dir}", _env_file=None)',
            "con = open_reader(settings)",
            textwrap.dedent(body).strip(),
        ]
    )
    env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120
    )


def test_reader_works_while_the_write_lock_is_held(warehouse: Settings) -> None:
    """The headline requirement: the UI keeps working mid-ingest."""
    with write_conn(warehouse) as writer:
        writer.execute("CREATE TABLE ingest_in_progress (x INTEGER)")
        result = _run_reader(
            warehouse,
            """
            rows = con.execute(
                "SELECT value FROM fundamentals_asof(DATE '2026-01-01') "
                "WHERE canonical = 'revenue' AND duration_bucket = 'annual'"
            ).fetchall()
            print("VALUE", rows[0][0])
            """,
        )
    assert result.returncode == 0, result.stderr
    assert "VALUE 175.0" in result.stdout


def test_the_writer_really_was_holding_the_lock(warehouse: Settings) -> None:
    """Guards the test above: if the lock were free, it would prove nothing."""
    with write_conn(warehouse):
        result = _run_reader(
            warehouse,
            """
            from finlab.db.conn import WriterLockHeld, write_conn
            try:
                with write_conn(settings):
                    print("SECOND WRITER OPENED")
            except WriterLockHeld:
                print("REFUSED")
            """,
        )
    assert result.returncode == 0, result.stderr
    assert "REFUSED" in result.stdout


def test_the_reader_never_opens_the_database_file(warehouse: Settings) -> None:
    """Even with no writer around, the read path must stay on Parquet.

    Checked by deleting the DuckDB file outright: if the reader needed it, this
    would fail.
    """
    with write_conn(warehouse):
        pass
    warehouse.db_path.unlink(missing_ok=True)

    result = _run_reader(
        warehouse,
        """
        rows = con.execute(
            "SELECT count(*) FROM fundamentals_asof(DATE '2026-01-01')"
        ).fetchall()
        print("ROWS", rows[0][0])
        """,
    )
    assert result.returncode == 0, result.stderr
    assert "ROWS 2" in result.stdout, "one annual row and one quarterly row"


def test_as_of_selects_the_vintage_that_was_public(warehouse: Settings) -> None:
    """The slider's whole purpose, exercised through the app's query layer."""
    from finlab.ui.queries import open_reader

    con = open_reader(warehouse)
    try:
        before = con.execute(
            "SELECT value FROM fundamentals_asof(?) WHERE canonical = 'revenue' "
            "AND duration_bucket = 'annual'",
            [date(2025, 1, 1)],
        ).fetchone()
        after = con.execute(
            "SELECT value FROM fundamentals_asof(?) WHERE canonical = 'revenue' "
            "AND duration_bucket = 'annual'",
            [date(2026, 1, 1)],
        ).fetchone()
    finally:
        con.close()

    assert before[0] == 100.0, "as-of before the restatement must show the original"
    assert after[0] == 175.0, "as-of after the restatement must show the revision"


def test_restatement_history_does_not_mix_reporting_windows(warehouse: Settings) -> None:
    """A 10-K tags the same concept over the year and the closing quarter.

    Reading them as one series invents a restatement that never happened — the
    figure appears to swing between the annual and quarterly value on the same
    filing date.
    """
    from finlab.ui.queries import open_reader, restatement_history

    con = open_reader(warehouse)
    try:
        annual = restatement_history(
            con, 320193, "revenue", date(2024, 9, 28), date(2026, 1, 1), "annual"
        )
        quarterly = restatement_history(
            con, 320193, "revenue", date(2024, 9, 28), date(2026, 1, 1), "quarter"
        )
    finally:
        con.close()

    assert annual["value"].to_list() == [100.0, 175.0]
    assert quarterly["value"].to_list() == [40.0]
