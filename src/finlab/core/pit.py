"""The `fundamentals_asof(as_of)` macro and Q4 derivation.

`fundamentals_asof` is the only sanctioned way to read fundamentals. Its single
`WHERE f.filed_date <= as_of` is the difference between a real backtest and a
fantasy, and `tests/test_pit.py` exists to prove it holds.

Q4 is never filed as a standalone quarter, so for flow items it has to be
derived as `FY - (Q1 + Q2 + Q3)`. The subtlety is that this derivation is itself
point-in-time: the annual figure and the three quarters each have their own
filing history, so a derived Q4 is only as good as the vintages available when
the annual figure was filed. `build_q4_table` therefore materialises one derived
row *per annual filing vintage*, carrying that filing's `filed_date` and
`accession_number`. Those rows are then filtered by `filed_date <= as_of`
exactly like real facts, so the derivation cannot leak the future. A single
static Q4 table with no filing date would have been simpler and wrong.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import duckdb

from finlab.config import Settings
from finlab.db.conn import read_conn

# Reproduced from docs/architecture.md §3.6, including the trailing `rn` column.
FUNDAMENTALS_ASOF_SQL = """
CREATE OR REPLACE MACRO fundamentals_asof(as_of) AS TABLE
  SELECT * FROM (
    SELECT
      f.cik, cm.canonical, f.period_start, f.period_end,
      f.duration_bucket, f.value * cm.sign AS value,
      f.filed_date, f.accession_number,
      ROW_NUMBER() OVER (
        PARTITION BY f.cik, cm.canonical, f.period_end, f.duration_bucket
        ORDER BY f.filed_date DESC, cm.priority ASC, f.accession_number DESC
      ) AS rn
    FROM fact f
    JOIN concept_map cm
      ON cm.taxonomy = f.taxonomy AND cm.tag = f.tag
     AND f.filed_date BETWEEN cm.valid_from AND cm.valid_to
    WHERE f.filed_date <= as_of
  ) WHERE rn = 1
"""

# Concepts recoverable from accounting identities when a filer does not tag them
# directly.
#
# Two identities, both exact rather than approximate:
#   total_liabilities = total_assets - equity
#   gross_profit      = revenue - cogs
#
# These are not a fallback guess. Roughly a third of the universe never tags
# `Liabilities` at all, reporting only assets and equity, and about half never
# tag `GrossProfit`. Deriving is the difference between "this company has no
# balance sheet" and reading the balance sheet it actually filed.
#
# Point-in-time safety is automatic: both inputs come from the same
# `fundamentals_asof(as_of)` call, so a derived value can only be built from
# facts that were already public. The derived row inherits the LATER of its two
# inputs' filed_dates, because that is when it first became computable.
#
# A directly-reported value always wins; derivation fills gaps only.
FUNDAMENTALS_COMPLETE_ASOF_SQL = """
CREATE OR REPLACE MACRO fundamentals_complete_asof(as_of) AS TABLE
  WITH base AS (
    SELECT cik, canonical, period_start, period_end, duration_bucket, value,
           filed_date, accession_number
    FROM fundamentals_asof(as_of)
  ),
  derived AS (
    SELECT l.cik, 'total_liabilities' AS canonical, l.period_start, l.period_end,
           l.duration_bucket, l.value - r.value AS value,
           greatest(l.filed_date, r.filed_date) AS filed_date, l.accession_number
    FROM base l
    JOIN base r ON r.cik = l.cik AND r.period_end = l.period_end
               AND r.duration_bucket = l.duration_bucket AND r.canonical = 'equity'
    WHERE l.canonical = 'total_assets'
      AND NOT EXISTS (
        SELECT 1 FROM base x WHERE x.cik = l.cik AND x.period_end = l.period_end
          AND x.duration_bucket = l.duration_bucket AND x.canonical = 'total_liabilities')
    UNION ALL
    SELECT l.cik, 'gross_profit', l.period_start, l.period_end,
           l.duration_bucket, l.value - r.value,
           greatest(l.filed_date, r.filed_date), l.accession_number
    FROM base l
    JOIN base r ON r.cik = l.cik AND r.period_end = l.period_end
               AND r.duration_bucket = l.duration_bucket AND r.canonical = 'cogs'
    WHERE l.canonical = 'revenue'
      AND NOT EXISTS (
        SELECT 1 FROM base x WHERE x.cik = l.cik AND x.period_end = l.period_end
          AND x.duration_bucket = l.duration_bucket AND x.canonical = 'gross_profit')
  )
  SELECT *, FALSE AS derived FROM base
  UNION ALL
  SELECT *, TRUE AS derived FROM derived
"""

# Real quarters plus derived Q4, both point-in-time filtered. Where a company
# genuinely files a standalone Q4, the real row wins over the derived one.
QUARTERLY_ASOF_SQL = """
CREATE OR REPLACE MACRO quarterly_asof(as_of) AS TABLE
  SELECT * EXCLUDE (rn) FROM (
    SELECT cik, canonical, period_start, period_end, value, filed_date,
           accession_number, derived,
           ROW_NUMBER() OVER (
             PARTITION BY cik, canonical, period_end
             ORDER BY derived ASC, filed_date DESC, accession_number DESC
           ) AS rn
    FROM (
      SELECT cik, canonical, period_start, period_end, value, filed_date,
             accession_number, FALSE AS derived
      FROM fundamentals_asof(as_of)
      WHERE duration_bucket = 'quarter'
      UNION ALL
      SELECT cik, canonical, period_start, period_end, value, filed_date,
             accession_number, TRUE AS derived
      FROM fundamentals_q4
      WHERE filed_date <= as_of
    )
  ) WHERE rn = 1
"""

# One derived Q4 row per (company, concept, annual filing vintage).
#
# `best` collapses competing tags within a single vintage using concept_map
# priority, so a company reporting both `Revenues` and the ASC 606 tag in one
# filing contributes one value, not two. The join then pairs each annual figure
# with the latest vintage of each of its three quarters that existed when the
# annual figure was filed — never a later revision.
BUILD_Q4_SQL = """
CREATE OR REPLACE TABLE fundamentals_q4 AS
WITH resolved AS (
  SELECT f.cik, cm.canonical, f.period_start, f.period_end, f.duration_bucket,
         f.value * cm.sign AS value, f.filed_date, f.accession_number, cm.priority
  FROM fact f
  JOIN concept_map cm
    ON cm.taxonomy = f.taxonomy AND cm.tag = f.tag
   AND f.filed_date BETWEEN cm.valid_from AND cm.valid_to
  WHERE f.duration_bucket IN ('annual', 'quarter')
),
best AS (
  SELECT * EXCLUDE (rn) FROM (
    SELECT *, ROW_NUMBER() OVER (
      PARTITION BY cik, canonical, period_end, duration_bucket, filed_date
      ORDER BY priority ASC, accession_number DESC
    ) AS rn
    FROM resolved
  ) WHERE rn = 1
),
annual AS (SELECT * FROM best WHERE duration_bucket = 'annual'),
quarter AS (SELECT * FROM best WHERE duration_bucket = 'quarter'),
paired AS (
  SELECT * EXCLUDE (rn) FROM (
    SELECT a.cik, a.canonical,
           a.period_start AS fy_start, a.period_end AS fy_end,
           a.value AS fy_value, a.filed_date AS fy_filed,
           a.accession_number AS fy_accession,
           q.period_end AS q_end, q.value AS q_value,
           ROW_NUMBER() OVER (
             PARTITION BY a.cik, a.canonical, a.period_end, a.filed_date, q.period_end
             ORDER BY q.filed_date DESC, q.accession_number DESC
           ) AS rn
    FROM annual a
    JOIN quarter q
      ON q.cik = a.cik AND q.canonical = a.canonical
     AND q.period_end > a.period_start AND q.period_end < a.period_end
     AND q.filed_date <= a.filed_date
  ) WHERE rn = 1
)
SELECT cik, canonical,
       (max(q_end) + INTERVAL 1 DAY)::DATE AS period_start,
       fy_end AS period_end,
       'quarter' AS duration_bucket,
       any_value(fy_value) - sum(q_value) AS value,
       fy_filed AS filed_date,
       fy_accession AS accession_number
FROM paired
GROUP BY cik, canonical, fy_start, fy_end, fy_filed, fy_accession
HAVING count(*) = 3
"""


def _relation_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    try:
        con.execute(f"SELECT 1 FROM {name} LIMIT 0")
    except duckdb.CatalogException:
        return False
    return True


def install_macros(con: duckdb.DuckDBPyConnection) -> tuple[str, ...]:
    """Register the point-in-time macros on a connection. Returns what was installed.

    Every reader calls this, including the in-memory connections the UI and
    notebooks open over the Parquet warehouse. `fundamentals_asof` is the
    contract `tests/test_pit.py` is written against and must not change shape.

    `quarterly_asof` is installed only where the derived Q4 table is present:
    DuckDB resolves table references when a macro is created, not when it is
    called, so registering it against a missing table fails outright.
    """
    con.execute(FUNDAMENTALS_ASOF_SQL)
    con.execute(FUNDAMENTALS_COMPLETE_ASOF_SQL)
    installed = ["fundamentals_asof", "fundamentals_complete_asof"]
    if _relation_exists(con, "fundamentals_q4"):
        con.execute(QUARTERLY_ASOF_SQL)
        installed.append("quarterly_asof")
    return tuple(installed)


def build_q4_table(con: duckdb.DuckDBPyConnection) -> int:
    """Materialise derived Q4 rows. Requires `fact` and `concept_map`."""
    # A reader connection already exposes last run's output as a warehouse view,
    # and DuckDB will not replace a view with a table of the same name.
    con.execute("DROP VIEW IF EXISTS fundamentals_q4")
    con.execute(BUILD_Q4_SQL)
    return int(con.execute("SELECT count(*) FROM fundamentals_q4").fetchone()[0])


@contextmanager
def analytics_conn(settings: Settings | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
    """The standard read path: in-memory DuckDB over Parquet, macros installed.

    This is what every consumer of fundamentals uses — the UI, notebooks, the
    coverage check, the metric builder. It never opens the DuckDB file, so it
    works while the daemon holds the write lock.
    """
    with read_conn(settings) as con:
        install_macros(con)
        yield con
