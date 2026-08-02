"""The concept map and the Q4 derivation."""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

import duckdb
import pytest

from finlab.core.concepts import (
    ConceptMapError,
    canonical_concepts,
    load_concept_map_file,
)
from finlab.core.pit import build_q4_table, install_macros
from finlab.ingest.universe import UniverseError, load_poc_universe, sectors

EXPECTED_CONCEPTS = {
    "revenue",
    "cogs",
    "gross_profit",
    "operating_income",
    "net_income",
    "total_assets",
    "total_liabilities",
    "equity",
    "cash",
    "total_debt",
    "cfo",
    "capex",
}


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "concept_map.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --- the shipped file -------------------------------------------------------


def test_the_shipped_map_declares_exactly_the_twelve_concepts() -> None:
    assert set(canonical_concepts()) == EXPECTED_CONCEPTS


def test_revenue_needs_several_tags() -> None:
    """One tag is never enough: ASC 606 replaced SalesRevenue* in 2018 and
    financial issuers used neither."""
    revenue = [r for r in load_concept_map_file() if r.canonical == "revenue"]
    assert len(revenue) >= 4
    tags = {r.tag for r in revenue}
    assert "Revenues" in tags
    assert "RevenueFromContractWithCustomerExcludingAssessedTax" in tags
    assert "RevenuesNetOfInterestExpense" in tags, "banks resolve to nothing without this"


def test_revenues_outranks_the_asc_606_tag() -> None:
    """Both appear in one filing for some issuers, where the ASC 606 tag can be
    only the contract-revenue component (American Tower: 936M vs a 10.6B total)."""
    by_tag = {r.tag: r.priority for r in load_concept_map_file() if r.canonical == "revenue"}
    assert by_tag["Revenues"] < by_tag["RevenueFromContractWithCustomerExcludingAssessedTax"]


def test_every_concept_declares_a_kind() -> None:
    assert {r.kind for r in load_concept_map_file()} == {"flow", "instant"}


# --- validation -------------------------------------------------------------


def test_duplicate_priority_is_rejected(tmp_path: Path) -> None:
    """Two tags at the same priority makes resolution arbitrary."""
    path = _write(
        tmp_path,
        """
        taxonomy: us-gaap
        concepts:
          revenue:
            description: x
            kind: flow
            tags:
              - { tag: Revenues, priority: 1 }
              - { tag: SalesRevenueNet, priority: 1 }
        """,
    )
    with pytest.raises(ConceptMapError, match="reuses priority"):
        load_concept_map_file(path)


def test_missing_kind_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        taxonomy: us-gaap
        concepts:
          revenue:
            description: x
            tags:
              - { tag: Revenues, priority: 1 }
        """,
    )
    with pytest.raises(ConceptMapError, match="kind"):
        load_concept_map_file(path)


def test_concept_without_tags_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        taxonomy: us-gaap
        concepts:
          revenue:
            description: x
            kind: flow
            tags: []
        """,
    )
    with pytest.raises(ConceptMapError, match="no tags"):
        load_concept_map_file(path)


# --- the PoC universe -------------------------------------------------------


def test_universe_spans_at_least_six_sectors() -> None:
    """Peer comparison is meaningless on a single-sector sample."""
    assert len(sectors()) >= 6


def test_universe_has_around_fifty_companies() -> None:
    assert 45 <= len(load_poc_universe()) <= 60


def test_security_id_is_built_from_cik_not_ticker() -> None:
    """Tickers get recycled; CIKs do not."""
    apple = next(c for c in load_poc_universe() if c.ticker == "AAPL")
    assert apple.security_id == "US:0000320193:AAPL"


def test_duplicate_cik_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "universe.yaml"
    path.write_text(
        textwrap.dedent(
            """
            companies:
              - { cik: 1, ticker: "A", sector: "S", name: "A Inc" }
              - { cik: 1, ticker: "B", sector: "S", name: "B Inc" }
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(UniverseError, match="appears twice"):
        load_poc_universe(path)


# --- Q4 derivation ----------------------------------------------------------


@pytest.fixture
def q4_con() -> duckdb.DuckDBPyConnection:
    """A single company: FY 1000 filed with quarters 200/300/400 -> Q4 = 100."""
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE fact AS SELECT * FROM (VALUES
          (1, 'us-gaap', 'Revenues', 'USD', DATE '2024-01-01', DATE '2024-03-31', 90,
           'quarter', 200.0, 'q1', DATE '2024-04-30'),
          (1, 'us-gaap', 'Revenues', 'USD', DATE '2024-04-01', DATE '2024-06-30', 90,
           'quarter', 300.0, 'q2', DATE '2024-07-31'),
          (1, 'us-gaap', 'Revenues', 'USD', DATE '2024-07-01', DATE '2024-09-30', 91,
           'quarter', 400.0, 'q3', DATE '2024-10-31'),
          (1, 'us-gaap', 'Revenues', 'USD', DATE '2024-01-01', DATE '2024-12-31', 365,
           'annual', 1000.0, 'fy', DATE '2025-02-28')
        ) t(cik, taxonomy, tag, unit, period_start, period_end, duration_days,
            duration_bucket, value, accession_number, filed_date)
        """
    )
    con.execute(
        """
        CREATE TABLE concept_map AS
        SELECT 'revenue' AS canonical, 'us-gaap' AS taxonomy, 'Revenues' AS tag,
               1 AS priority, 1::SMALLINT AS sign,
               DATE '1900-01-01' AS valid_from, DATE '9999-12-31' AS valid_to
        """
    )
    return con


def test_q4_is_the_annual_less_the_three_quarters(q4_con: duckdb.DuckDBPyConnection) -> None:
    assert build_q4_table(q4_con) == 1
    row = q4_con.execute(
        "SELECT value, period_start, period_end, filed_date FROM fundamentals_q4"
    ).fetchone()
    assert row[0] == pytest.approx(100.0)
    assert row[1] == date(2024, 10, 1), "Q4 starts the day after Q3 ends"
    assert row[2] == date(2024, 12, 31)


def test_derived_q4_carries_the_annual_filings_date(
    q4_con: duckdb.DuckDBPyConnection,
) -> None:
    """Q4 only becomes computable when the annual figure is filed, so it must
    inherit that date or it would leak into earlier as-of queries."""
    build_q4_table(q4_con)
    filed = q4_con.execute("SELECT filed_date FROM fundamentals_q4").fetchone()[0]
    assert filed == date(2025, 2, 28)

    install_macros(q4_con)
    before = q4_con.execute(
        "SELECT count(*) FROM quarterly_asof(DATE '2025-01-01') WHERE derived"
    ).fetchone()[0]
    after = q4_con.execute(
        "SELECT count(*) FROM quarterly_asof(DATE '2025-06-01') WHERE derived"
    ).fetchone()[0]
    assert before == 0, "derived Q4 must not exist before the 10-K was filed"
    assert after == 1


def test_incomplete_years_produce_no_q4(q4_con: duckdb.DuckDBPyConnection) -> None:
    """Two quarters plus a year is not enough; guessing would be worse than a gap."""
    q4_con.execute("DELETE FROM fact WHERE accession_number = 'q2'")
    assert build_q4_table(q4_con) == 0


def test_instants_are_not_given_a_derived_q4(q4_con: duckdb.DuckDBPyConnection) -> None:
    q4_con.execute(
        """
        INSERT INTO fact VALUES
          (1, 'us-gaap', 'Revenues', 'USD', NULL, DATE '2024-12-31', NULL,
           'instant', 5000.0, 'bs', DATE '2025-02-28')
        """
    )
    build_q4_table(q4_con)
    buckets = {
        r[0]
        for r in q4_con.execute("SELECT DISTINCT duration_bucket FROM fundamentals_q4").fetchall()
    }
    assert buckets == {"quarter"}
