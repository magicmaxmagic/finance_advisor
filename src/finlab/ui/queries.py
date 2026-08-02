"""Read-only queries backing the UI.

Separated from the Streamlit layer so they can be tested without a browser —
in particular `tests/test_read_path.py`, which runs these while another process
holds the write lock.

Everything here goes through `fundamentals_complete_asof(as_of)`. No query
reads `fact` directly except the restatement history, which is *about* the
filing history and therefore has to see every vintage rather than the resolved
one.

**These return pandas, not polars**, which is the one sanctioned exception to
the project's polars default. Two reasons, both forced: Streamlit renders
through pandas regardless, and DuckDB's `.pl()` conversion segfaults inside
`polars.from_arrow` when called from Streamlit's script thread. Frames here are
at most a few thousand rows of already-aggregated output, so none of the memory
argument for polars applies. Transform code elsewhere stays on polars.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import duckdb
import pandas as pd

from finlab.config import Settings
from finlab.core.metrics import METRIC_EXPRESSIONS, install_metric_macros
from finlab.core.pit import install_macros
from finlab.db.conn import new_read_conn

METRIC_NAMES: tuple[str, ...] = tuple(METRIC_EXPRESSIONS)


def open_reader(settings: Settings | None = None) -> duckdb.DuckDBPyConnection:
    """An in-memory reader with every macro installed.

    Never opens the DuckDB file: the UI must keep working while `finlabd` holds
    the write lock.
    """
    con = new_read_conn(settings)
    install_macros(con)
    install_metric_macros(con)
    return con


@contextmanager
def cursor(con: duckdb.DuckDBPyConnection) -> Iterator[duckdb.DuckDBPyConnection]:
    """A short-lived child connection for one query.

    DuckDB connections are not thread-safe, and Streamlit runs each script rerun
    on its own thread while `st.cache_resource` hands every one of them the same
    connection object. Sharing it directly segfaults inside Arrow conversion
    under concurrent use. `cursor()` creates a child that shares the catalog —
    so the warehouse views and the point-in-time macros are all still there —
    while giving each query its own execution context.
    """
    child = con.cursor()
    try:
        yield child
    finally:
        child.close()


def has_prices(con: duckdb.DuckDBPyConnection) -> bool:
    """Whether a price series was ever ingested. See ingest/prices.py."""
    try:
        with cursor(con) as cur:
            cur.execute("SELECT 1 FROM price_daily LIMIT 1")
    except duckdb.CatalogException:
        return False
    return True


def data_range(con: duckdb.DuckDBPyConnection) -> tuple[date, date]:
    """Earliest and latest filing dates in the warehouse."""
    with cursor(con) as cur:
        first, last = cur.execute("SELECT min(filed_date), max(filed_date) FROM fact").fetchone()
    return first, last


def sectors(con: duckdb.DuckDBPyConnection) -> list[str]:
    with cursor(con) as cur:
        return [
            r[0] for r in cur.execute("SELECT DISTINCT sector FROM entity ORDER BY 1").fetchall()
        ]


def companies(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    with cursor(con) as cur:
        return cur.execute(
            """
        SELECT e.cik, coalesce(s.ticker, '?') AS ticker, e.name, e.sector
        FROM entity e LEFT JOIN security s USING (cik)
        ORDER BY ticker
        """
        ).df()


def screen(con: duckdb.DuckDBPyConnection, as_of: date) -> pd.DataFrame:
    """One row per company at `as_of`: identity, size, and every metric."""
    pivot = ",\n               ".join(
        f"max(m.value) FILTER (m.metric_name = '{name}') AS {name}" for name in METRIC_NAMES
    )
    # The price feed is optional: Stooq currently refuses automated access, so
    # market cap is unavailable rather than wrong. Everything else on the screen
    # is fundamentals-only and unaffected.
    if has_prices(con):
        price_cte = (
            "SELECT s.cik, last(p.close ORDER BY p.date) AS close "
            "FROM price_daily p JOIN security s USING (security_id) "
            "WHERE p.date <= ? GROUP BY s.cik"
        )
        params = [as_of, as_of, as_of, as_of]
    else:
        price_cte = "SELECT NULL::INTEGER AS cik, NULL::DOUBLE AS close WHERE FALSE"
        params = [as_of, as_of, as_of]
    with cursor(con) as cur:
        return cur.execute(
            """
        WITH size AS (
          SELECT cik,
                 max(value) FILTER (canonical = 'revenue') AS revenue,
                 max(value) FILTER (canonical = 'total_assets') AS total_assets
          FROM fundamentals_latest_annual(?)
          GROUP BY cik
        ),
        shares AS (
          -- Shares outstanding come from the dei taxonomy, so they survive even
          -- though the price feed does not.
          SELECT cik, last(value ORDER BY period_end) AS shares_outstanding
          FROM fact
          WHERE taxonomy = 'dei' AND tag = 'EntityCommonStockSharesOutstanding'
            AND filed_date <= ?
          GROUP BY cik
        ),
        px AS (
          {price_cte}
        ),
        metrics AS (
          SELECT cik, PIVOT_COLUMNS FROM metrics_asof(?) m GROUP BY cik
        )
        SELECT sec.ticker, e.name, e.sector,
               px.close * sh.shares_outstanding AS market_cap,
               sz.revenue, sz.total_assets,
               m.* EXCLUDE (cik)
        FROM entity e
        LEFT JOIN security sec USING (cik)
        LEFT JOIN size sz USING (cik)
        LEFT JOIN shares sh USING (cik)
        LEFT JOIN px USING (cik)
        LEFT JOIN metrics m USING (cik)
        ORDER BY e.name
        """.replace("PIVOT_COLUMNS", pivot).format(price_cte=price_cte),
            params,
        ).df()


def company_history(
    con: duckdb.DuckDBPyConnection, cik: int, as_of: date, concepts: tuple[str, ...]
) -> pd.DataFrame:
    """Annual series for one company, with the filing each figure came from."""
    placeholders = ", ".join(["?"] * len(concepts))
    with cursor(con) as cur:
        return cur.execute(
            f"""
        SELECT f.canonical, f.period_end, f.value, f.filed_date, f.accession_number,
               coalesce(fl.form, '?') AS form, f.derived
        FROM fundamentals_complete_asof(?) f
        LEFT JOIN filing fl ON fl.accession_number = f.accession_number
        WHERE f.cik = ? AND f.duration_bucket = 'annual'
          AND f.canonical IN ({placeholders})
        ORDER BY f.period_end, f.canonical
        """,
            [as_of, cik, *concepts],
        ).df()


def company_metrics_with_percentile(
    con: duckdb.DuckDBPyConnection, cik: int, as_of: date
) -> pd.DataFrame:
    """Each metric for one company, with its percentile inside its own sector."""
    with cursor(con) as cur:
        return cur.execute(
            """
        WITH m AS (
          SELECT m.cik, m.metric_name, m.value, e.sector
          FROM metrics_asof(?) m JOIN entity e USING (cik)
          WHERE m.value IS NOT NULL AND isfinite(m.value)
        ),
        ranked AS (
          SELECT *, percent_rank() OVER (
            PARTITION BY sector, metric_name ORDER BY value
          ) AS sector_percentile,
          count(*) OVER (PARTITION BY sector, metric_name) AS peers
          FROM m
        )
        SELECT metric_name, value, sector_percentile, peers
        FROM ranked WHERE cik = ? ORDER BY metric_name
        """,
            [as_of, cik],
        ).df()


def restatement_history(
    con: duckdb.DuckDBPyConnection,
    cik: int,
    canonical: str,
    period_end: date,
    as_of: date,
    duration_bucket: str = "annual",
) -> pd.DataFrame:
    """Every value ever reported for one (company, concept, period).

    This is the one place that reads `fact` directly rather than through the
    point-in-time macro: the whole point is to show the vintages the macro
    normally hides. `as_of` still applies, so moving the slider back shows what
    was knowable then.

    `duration_bucket` is not optional in spirit: a single filing tags the same
    concept over several windows (a 10-K carries both the year and the quarter),
    so without it the history interleaves incomparable figures and invents
    restatements that never happened.
    """
    with cursor(con) as cur:
        return cur.execute(
            """
        SELECT f.filed_date, f.accession_number, coalesce(fl.form, '?') AS form,
               f.tag, f.value * cm.sign AS value
        FROM fact f
        JOIN concept_map cm ON cm.taxonomy = f.taxonomy AND cm.tag = f.tag
                           AND f.filed_date BETWEEN cm.valid_from AND cm.valid_to
        LEFT JOIN filing fl ON fl.accession_number = f.accession_number
        WHERE f.cik = ? AND cm.canonical = ? AND f.period_end = ?
          AND f.filed_date <= ? AND f.duration_bucket = ?
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY f.filed_date, f.accession_number
          ORDER BY cm.priority ASC
        ) = 1
        ORDER BY f.filed_date, f.accession_number
        """,
            [cik, canonical, period_end, as_of, duration_bucket],
        ).df()


def restatement_candidates(con: duckdb.DuckDBPyConnection, cik: int) -> pd.DataFrame:
    """(concept, period) pairs for one company that were reported more than once."""
    with cursor(con) as cur:
        return cur.execute(
            """
        SELECT cm.canonical, f.period_end, f.duration_bucket,
               count(DISTINCT f.value) AS distinct_values,
               count(DISTINCT f.filed_date) AS filings,
               abs(max(f.value) - min(f.value)) AS spread
        FROM fact f
        JOIN concept_map cm ON cm.taxonomy = f.taxonomy AND cm.tag = f.tag
        WHERE f.cik = ? AND f.duration_bucket IN ('annual', 'quarter', 'instant')
        GROUP BY ALL
        HAVING count(DISTINCT f.value) > 1
        ORDER BY spread DESC
        """,
            [cik],
        ).df()


def concept_periods(con: duckdb.DuckDBPyConnection, cik: int, canonical: str) -> list[date]:
    with cursor(con) as cur:
        rows = cur.execute(
            """
        SELECT DISTINCT f.period_end
        FROM fact f
        JOIN concept_map cm ON cm.taxonomy = f.taxonomy AND cm.tag = f.tag
        WHERE f.cik = ? AND cm.canonical = ? AND f.duration_bucket = 'annual'
        ORDER BY 1 DESC
        """,
            [cik, canonical],
        ).fetchall()
    return [r[0] for r in rows]
