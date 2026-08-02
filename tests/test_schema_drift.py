"""`schema.sql` and the migration chain must describe the same database.

This is the test that makes baseline-plus-deltas safe. A fresh install applies
`schema.sql` in one shot; an existing install replays numbered deltas. Nothing
enforces that the two agree except this comparison, so if someone adds a
migration and forgets to update the baseline (or the reverse), the divergence
surfaces here rather than as a missing column three milestones later.
"""

from __future__ import annotations

import duckdb
import pytest

from finlab.db.migrations import (
    SCHEMA_PATH,
    apply_baseline,
    current_version,
    discover_migrations,
    replay_all,
)


def _columns(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute(
        """
        SELECT table_name, column_index, column_name, data_type, is_nullable, column_default
        FROM duckdb_columns()
        WHERE schema_name = 'main'
        ORDER BY table_name, column_index
        """
    ).fetchall()


def _constraints(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute(
        """
        SELECT table_name, constraint_type, constraint_column_names, constraint_text,
               referenced_table, referenced_column_names
        FROM duckdb_constraints()
        WHERE schema_name = 'main'
        ORDER BY ALL
        """
    ).fetchall()


def _indexes(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute(
        """
        SELECT index_name, table_name, is_unique, is_primary, sql
        FROM duckdb_indexes()
        WHERE schema_name = 'main'
        ORDER BY ALL
        """
    ).fetchall()


@pytest.fixture
def from_baseline() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    apply_baseline(con)
    return con


@pytest.fixture
def from_migrations() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    replay_all(con)
    return con


def test_there_is_something_to_compare() -> None:
    """Without a migration chain this whole module would pass vacuously."""
    assert discover_migrations(), "no migrations on disk — the drift test proves nothing"
    assert SCHEMA_PATH.is_file()


def test_columns_match(
    from_baseline: duckdb.DuckDBPyConnection, from_migrations: duckdb.DuckDBPyConnection
) -> None:
    baseline, replayed = _columns(from_baseline), _columns(from_migrations)
    assert baseline == replayed, (
        "schema.sql and the migration chain disagree on columns:\n"
        f"  only in schema.sql: {sorted(set(baseline) - set(replayed))}\n"
        f"  only in migrations: {sorted(set(replayed) - set(baseline))}"
    )


def test_constraints_match(
    from_baseline: duckdb.DuckDBPyConnection, from_migrations: duckdb.DuckDBPyConnection
) -> None:
    baseline, replayed = _constraints(from_baseline), _constraints(from_migrations)
    assert baseline == replayed, (
        "schema.sql and the migration chain disagree on constraints:\n"
        f"  only in schema.sql: {sorted(set(baseline) - set(replayed))}\n"
        f"  only in migrations: {sorted(set(replayed) - set(baseline))}"
    )


def test_indexes_match(
    from_baseline: duckdb.DuckDBPyConnection, from_migrations: duckdb.DuckDBPyConnection
) -> None:
    assert _indexes(from_baseline) == _indexes(from_migrations)


def test_both_paths_reach_the_same_version(
    from_baseline: duckdb.DuckDBPyConnection, from_migrations: duckdb.DuckDBPyConnection
) -> None:
    assert current_version(from_baseline) == current_version(from_migrations)


def test_the_comparison_actually_detects_a_difference(
    from_baseline: duckdb.DuckDBPyConnection,
) -> None:
    """Guard against the comparison silently returning equal for everything."""
    tampered = duckdb.connect(":memory:")
    replay_all(tampered)
    tampered.execute("ALTER TABLE fact ADD COLUMN sneaky_extra INTEGER")
    assert _columns(from_baseline) != _columns(tampered)
