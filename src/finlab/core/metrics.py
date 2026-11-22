"""Ratio computation into `metric`.

Every input is read through `fundamentals_complete_asof`, so a metric computed
for an as-of date can only use figures that were public on that date. Nothing
here calls an LLM: these are ratios, and a ratio that was generated rather than
computed is worthless.

Each metric carries `inputs_hash` (a hash of the exact source values) and
`code_version` (the git sha of the working tree), so a restatement or a formula
change invalidates precisely the affected rows instead of forcing a full
rebuild.

Basis: the latest **annual** period as of the query date, with balance-sheet
inputs taken at their latest instant. Annual rather than trailing-twelve-month
because cash-flow items arrive cumulatively (`half`, `ytd9`) rather than as
discrete quarters, and a TTM built by naive subtraction across those windows is
the classic way to get silently wrong numbers.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import date
from functools import lru_cache
from pathlib import Path

import duckdb
import polars as pl

from finlab import __version__
from finlab.db.keys import hash64_sql

# US federal statutory rate, used for the NOPAT numerator in ROIC. A per-company
# effective rate would be better and needs an income-tax concept the 12-concept
# map does not carry, so this is deliberately a flat approximation.
STATUTORY_TAX_RATE = 0.21

# The concepts each metric consumes, in the order they enter the hash.
METRIC_INPUTS: dict[str, tuple[str, ...]] = {
    "gross_margin": ("gross_profit", "revenue"),
    "operating_margin": ("operating_income", "revenue"),
    "net_margin": ("net_income", "revenue"),
    "fcf_margin": ("cfo", "capex", "revenue"),
    "roe": ("net_income", "equity"),
    "roa": ("net_income", "total_assets"),
    "roic": ("operating_income", "total_debt", "equity"),
    "net_debt_to_equity": ("total_debt", "cash", "equity"),
}

# Deliberately explicit rather than generated: these are the definitions a
# reader needs to audit, and they should be readable as arithmetic.
METRIC_EXPRESSIONS: dict[str, str] = {
    "gross_margin": "gross_profit / nullif(revenue, 0)",
    "operating_margin": "operating_income / nullif(revenue, 0)",
    "net_margin": "net_income / nullif(revenue, 0)",
    "fcf_margin": "(cfo - capex) / nullif(revenue, 0)",
    "roe": "net_income / nullif(equity, 0)",
    "roa": "net_income / nullif(total_assets, 0)",
    "roic": (f"(operating_income * (1 - {STATUTORY_TAX_RATE})) / nullif(total_debt + equity, 0)"),
    "net_debt_to_equity": "(total_debt - cash) / nullif(equity, 0)",
}

CONCEPTS: tuple[str, ...] = (
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
)

# Latest annual flow and latest instant per company, as of the query date.
LATEST_ANNUAL_SQL = """
CREATE OR REPLACE MACRO fundamentals_latest_annual(as_of) AS TABLE
  SELECT * EXCLUDE (rn) FROM (
    SELECT cik, canonical, period_end, duration_bucket, value, filed_date,
           accession_number,
           ROW_NUMBER() OVER (
             PARTITION BY cik, canonical ORDER BY period_end DESC, filed_date DESC
           ) AS rn
    FROM fundamentals_complete_asof(as_of)
    WHERE duration_bucket IN ('annual', 'instant')
  ) WHERE rn = 1
"""


def _wide_sql() -> str:
    columns = ",\n           ".join(
        f"max(value) FILTER (canonical = '{c}') AS {c}" for c in CONCEPTS
    )
    return (
        "CREATE OR REPLACE MACRO fundamentals_wide(as_of) AS TABLE\n"
        "  SELECT cik,\n"
        f"           {columns},\n"
        "           max(period_end) AS period_end,\n"
        "           max(filed_date) AS filed_date\n"
        "  FROM fundamentals_latest_annual(as_of)\n"
        "  GROUP BY cik"
    )


def _metrics_sql() -> str:
    blocks = []
    for name, expression in METRIC_EXPRESSIONS.items():
        inputs = METRIC_INPUTS[name]
        inputs_hash = hash64_sql(inputs)
        blocks.append(
            f"    SELECT cik, '{name}' AS metric_name, ({expression})::DOUBLE AS value,\n"
            f"           {inputs_hash} AS inputs_hash, period_end, filed_date\n"
            f"    FROM w"
        )
    body = "\n    UNION ALL\n".join(blocks)
    return (
        "CREATE OR REPLACE MACRO metrics_asof(as_of) AS TABLE\n"
        "  WITH w AS (SELECT * FROM fundamentals_wide(as_of))\n"
        f"{body}"
    )


def install_metric_macros(con: duckdb.DuckDBPyConnection) -> None:
    """Register `fundamentals_latest_annual`, `fundamentals_wide`, `metrics_asof`.

    Requires the point-in-time macros to be installed first.
    """
    con.execute(LATEST_ANNUAL_SQL)
    con.execute(_wide_sql())
    con.execute(_metrics_sql())


@lru_cache(maxsize=1)
def code_version() -> str:
    """The git sha of the working tree, falling back to the package version."""
    revision = f"v{__version__}"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        pass
    else:
        revision = result.stdout.strip() or revision
    digest = hashlib.sha256()
    for source in (Path(__file__), Path(__file__).with_name("pit.py")):
        digest.update(source.read_bytes())
    return revision + "-" + digest.hexdigest()[:16]


def compute_metrics(con: duckdb.DuckDBPyConnection, as_of: date) -> pl.DataFrame:
    """Compute every metric for every company at `as_of`. Reader connection."""
    return con.execute(
        "SELECT cik, metric_name, value, inputs_hash FROM metrics_asof(?) ORDER BY cik, metric_name",
        [as_of],
    ).pl()


def store_metrics(con: duckdb.DuckDBPyConnection, frame: pl.DataFrame, as_of: date) -> int:
    """Write a computed snapshot into `metric`. Write connection."""
    con.register("_metrics", frame)
    con.execute(
        """
        INSERT OR REPLACE INTO metric
          (cik, as_of_date, metric_name, value, inputs_hash, code_version, computed_at)
        SELECT cik, ?, metric_name, value, CAST(inputs_hash AS VARCHAR), ?, now()::TIMESTAMP
        FROM _metrics
        """,
        [as_of, code_version()],
    )
    con.unregister("_metrics")
    return int(
        con.execute("SELECT count(*) FROM metric WHERE as_of_date = ?", [as_of]).fetchone()[0]
    )
