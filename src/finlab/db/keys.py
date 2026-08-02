"""Deterministic 64-bit surrogate keys for the warehouse tables.

`fact` and `price_daily` have no physical primary key: their logical keys
contain a nullable column, and an ART index over 10^8 rows would not fit the
memory budget (see schema.sql). Instead each row carries a hash of its logical
key, so deduplication is one `GROUP BY` and a restatement check is one
`HAVING count(DISTINCT value) > 1`.

**Why MD5 and not DuckDB's `hash()`.** These keys are persisted into Parquet and
compared against partitions written months earlier. `hash()` is an internal
function with no cross-version stability guarantee, so a DuckDB upgrade could
silently stop matching old partitions against new ones. MD5 is a fixed
standard: the same input yields the same digest forever. Only the low 64 bits
are kept — this is a dedup key, not a cryptographic commitment, and collisions
at 10^8 rows sit around 3e-4 by the birthday bound.

The hash is computed in SQL, never in Python, so no column is ever
materialised into process memory.
"""

from __future__ import annotations

# The logical key of `fact`, in the order the architecture document lists it.
FACT_KEY_FIELDS: tuple[str, ...] = (
    "cik",
    "taxonomy",
    "tag",
    "unit",
    "period_start",
    "period_end",
    "accession_number",
)

# The logical key of `price_daily`. The architecture document names the column
# `date`; the field list is otherwise as specified.
PRICE_KEY_FIELDS: tuple[str, ...] = ("security_id", "date")

# concat_ws drops NULL arguments outright, so an instant fact (period_start
# NULL) would hash identically to a duration fact with an empty period_start.
# An explicit sentinel keeps them distinct.
#
# Both are ASCII control characters emitted by chr() rather than written as
# literals: a literal control byte in the SQL text breaks the parser, and no
# source field (CIK, taxonomy slug, tag, unit, ISO date, accession number) can
# ever contain one, so neither the separator nor the sentinel is ambiguous.
_SEPARATOR = "chr(31)"  # ASCII unit separator
_NULL_SENTINEL = "chr(30)"  # ASCII record separator


def hash64_sql(fields: tuple[str, ...], *, prefix: str = "") -> str:
    """A SQL expression hashing `fields` to a signed 64-bit key.

    `prefix` qualifies the column names (e.g. `"f."`). The result is a BIGINT:
    MD5's low 64 bits are unsigned, so values at or above 2^63 are
    reinterpreted as their two's-complement negatives, which preserves every
    bit rather than clamping.
    """
    parts = ", ".join(
        f"coalesce(CAST({prefix}{field} AS VARCHAR), {_NULL_SENTINEL})" for field in fields
    )
    digest = f"md5_number_lower(concat_ws({_SEPARATOR}, {parts}))"
    return (
        f"CAST(CASE WHEN {digest} >= 9223372036854775808::UBIGINT "
        f"THEN CAST({digest} AS HUGEINT) - 18446744073709551616 "
        f"ELSE CAST({digest} AS HUGEINT) END AS BIGINT)"
    )


def fact_key_sql(prefix: str = "") -> str:
    return hash64_sql(FACT_KEY_FIELDS, prefix=prefix)


def price_key_sql(prefix: str = "") -> str:
    return hash64_sql(PRICE_KEY_FIELDS, prefix=prefix)
