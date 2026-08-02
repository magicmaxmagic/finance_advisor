"""The point-in-time regression test.

CLAUDE.md, Testing: for a set of random `(cik, as_of_date)` pairs, no row
returned by `fundamentals_asof` may have `filed_date > as_of_date`. This test
may never be skipped or weakened.

Status at M0: `fundamentals_asof` does not exist yet — it lands in M2 — so the
test cannot run. It is written in full against the contract M2 must satisfy:

    finlab.core.pit.install_macros(con)   # registers fundamentals_asof(as_of)

and activates automatically as soon as that function exists. The assertions
below are the real ones; nothing here is a placeholder. The synthetic fixture
deliberately includes restatements, instants and multiple competing tags, since
those are the cases where a naive implementation leaks future data.
"""

from __future__ import annotations

import datetime as dt
import random

import duckdb
import pytest

SEED = 20260802
N_COMPANIES = 40
N_PROBES = 200

pit = pytest.importorskip("finlab.core.pit", reason="the PIT layer lands in M2")
install_macros = getattr(pit, "install_macros", None)

pytestmark = pytest.mark.skipif(
    install_macros is None,
    reason=(
        "finlab.core.pit.install_macros is not implemented yet (M2). "
        "This test activates the moment it exists and must not be removed."
    ),
)


def _build_fixture(con: duckdb.DuckDBPyConnection, rng: random.Random) -> list[int]:
    """A small warehouse containing exactly the shapes that break naive PIT code."""
    con.execute(
        """
        CREATE TABLE fact (
          cik INTEGER, taxonomy VARCHAR, tag VARCHAR, unit VARCHAR,
          period_start DATE, period_end DATE, duration_days INTEGER,
          duration_bucket VARCHAR, value DOUBLE, accession_number VARCHAR,
          form VARCHAR, fiscal_year INTEGER, fiscal_period VARCHAR,
          filed_date DATE, frame VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE concept_map (
          canonical VARCHAR, taxonomy VARCHAR, tag VARCHAR, priority INTEGER,
          sign SMALLINT, valid_from DATE, valid_to DATE
        )
        """
    )
    con.executemany(
        "INSERT INTO concept_map VALUES (?, 'us-gaap', ?, ?, 1, DATE '1900-01-01', DATE '9999-12-31')",
        [
            ("revenue", "Revenues", 2),
            ("revenue", "RevenueFromContractWithCustomerExcludingAssessedTax", 1),
            ("total_assets", "Assets", 1),
        ],
    )

    ciks = [320193 + i for i in range(N_COMPANIES)]
    rows = []
    for cik in ciks:
        for quarter in range(20):
            period_end = dt.date(2020, 1, 1) + dt.timedelta(days=91 * quarter)
            period_start = period_end - dt.timedelta(days=90)
            # Original filing, then one or two restatements of the same period.
            for revision in range(rng.randint(1, 3)):
                filed = period_end + dt.timedelta(days=30 + 180 * revision)
                accession = f"{cik:010d}-{quarter:02d}-{revision}"
                tag = rng.choice(
                    ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"]
                )
                rows.append(
                    (
                        cik,
                        "us-gaap",
                        tag,
                        "USD",
                        period_start,
                        period_end,
                        90,
                        "quarter",
                        1000.0 + revision,
                        accession,
                        "10-Q",
                        period_end.year,
                        f"Q{(quarter % 4) + 1}",
                        filed,
                        None,
                    )
                )
                # An instant fact for the same filing: period_start is NULL.
                rows.append(
                    (
                        cik,
                        "us-gaap",
                        "Assets",
                        "USD",
                        None,
                        period_end,
                        None,
                        "instant",
                        50_000.0 + revision,
                        accession,
                        "10-Q",
                        period_end.year,
                        f"Q{(quarter % 4) + 1}",
                        filed,
                        None,
                    )
                )
    con.executemany("INSERT INTO fact VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return ciks


def test_fundamentals_asof_never_returns_a_future_filing() -> None:
    rng = random.Random(SEED)
    con = duckdb.connect(":memory:")
    ciks = _build_fixture(con, rng)
    install_macros(con)

    for _ in range(N_PROBES):
        cik = rng.choice(ciks)
        as_of = dt.date(2020, 1, 1) + dt.timedelta(days=rng.randint(0, 6 * 365))
        leaked = con.execute(
            "SELECT count(*) FROM fundamentals_asof(?) WHERE cik = ? AND filed_date > ?",
            [as_of, cik, as_of],
        ).fetchone()[0]
        assert leaked == 0, f"look-ahead leak for cik={cik} as_of={as_of}: {leaked} rows"


def test_fundamentals_asof_returns_the_latest_filing_not_the_first() -> None:
    """PIT means "as known then", not "as first reported"."""
    rng = random.Random(SEED)
    con = duckdb.connect(":memory:")
    _build_fixture(con, rng)
    install_macros(con)

    row = con.execute(
        """
        SELECT f.filed_date, max(a.filed_date)
        FROM fundamentals_asof(DATE '2026-01-01') f
        JOIN fact a
          ON a.cik = f.cik AND a.period_end = f.period_end
         AND a.filed_date <= DATE '2026-01-01'
        WHERE f.canonical = 'revenue'
        GROUP BY f.cik, f.period_end, f.filed_date
        HAVING f.filed_date <> max(a.filed_date)
        LIMIT 1
        """
    ).fetchone()
    assert row is None, f"stale revision selected: {row}"


def test_instant_facts_survive_the_pit_layer() -> None:
    """Balance-sheet facts have a NULL period_start and must not be dropped."""
    rng = random.Random(SEED)
    con = duckdb.connect(":memory:")
    _build_fixture(con, rng)
    install_macros(con)

    count = con.execute(
        "SELECT count(*) FROM fundamentals_asof(DATE '2026-01-01') "
        "WHERE canonical = 'total_assets' AND duration_bucket = 'instant'"
    ).fetchone()[0]
    assert count > 0
