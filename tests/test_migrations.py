from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from finlab.config import Settings
from finlab.db.conn import write_conn
from finlab.db.migrations import (
    MigrationError,
    apply_baseline,
    current_version,
    discover_migrations,
    init_database,
    migrate,
)


def test_discovery_of_an_empty_directory(tmp_path: Path) -> None:
    assert discover_migrations(tmp_path) == []


def test_migrations_are_ordered_by_version(tmp_path: Path) -> None:
    for name in ("0002_second.sql", "0001_first.sql", "0010_tenth.sql"):
        (tmp_path / name).write_text("SELECT 1;")
    assert [m.version for m in discover_migrations(tmp_path)] == [1, 2, 10]


def test_a_misnamed_migration_is_an_error(tmp_path: Path) -> None:
    """Skipping it quietly would mean it silently never runs."""
    (tmp_path / "add_thing.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="must be named"):
        discover_migrations(tmp_path)


def test_duplicate_version_numbers_are_an_error(tmp_path: Path) -> None:
    (tmp_path / "0001_one.sql").write_text("SELECT 1;")
    (tmp_path / "0001_other.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="duplicate migration number"):
        discover_migrations(tmp_path)


def test_uninitialised_database_reports_version_minus_one() -> None:
    con = duckdb.connect(":memory:")
    assert current_version(con) == -1


def test_init_applies_the_baseline(settings: Settings) -> None:
    settings.ensure_dirs()
    with write_conn(settings) as con:
        result = init_database(con)
        assert result.baseline_applied
        assert result.migrations_applied == ()
        assert current_version(con) == result.version


def test_init_is_idempotent(settings: Settings) -> None:
    settings.ensure_dirs()
    with write_conn(settings) as con:
        first = init_database(con)
    with write_conn(settings) as con:
        second = init_database(con)
        assert not second.baseline_applied
        assert second.migrations_applied == ()
        assert second.version == first.version


def test_baseline_is_stamped_at_the_highest_migration_number(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh install must not replay migrations schema.sql already contains."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_a.sql").write_text("CREATE TABLE should_not_run_a (x INTEGER);")
    (migrations / "0002_b.sql").write_text("CREATE TABLE should_not_run_b (x INTEGER);")
    monkeypatch.setattr("finlab.db.migrations.MIGRATIONS_DIR", migrations)

    settings.ensure_dirs()
    with write_conn(settings) as con:
        version = apply_baseline(con)
        assert version == 2
        assert migrate(con) == []
        tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
    assert "should_not_run_a" not in tables


def test_migrate_applies_only_versions_above_the_watermark(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    monkeypatch.setattr("finlab.db.migrations.MIGRATIONS_DIR", migrations)

    settings.ensure_dirs()
    with write_conn(settings) as con:
        assert apply_baseline(con) == 0

    # A migration arrives after the database was created.
    (migrations / "0001_add_watchlist.sql").write_text(
        "CREATE TABLE watchlist (cik INTEGER PRIMARY KEY);"
    )
    with write_conn(settings) as con:
        applied = migrate(con)
        assert [m.version for m in applied] == [1]
        assert current_version(con) == 1
        con.execute("SELECT count(*) FROM watchlist")
        # Running again is a no-op.
        assert migrate(con) == []


def test_a_failing_migration_rolls_back_and_does_not_stamp(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    monkeypatch.setattr("finlab.db.migrations.MIGRATIONS_DIR", migrations)

    settings.ensure_dirs()
    with write_conn(settings) as con:
        apply_baseline(con)

    (migrations / "0001_broken.sql").write_text("CREATE TABLE good (x INTEGER); THIS IS NOT SQL;")
    with write_conn(settings) as con:
        with pytest.raises(MigrationError, match=r"0001_broken\.sql failed"):
            migrate(con)
        assert current_version(con) == 0
        tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        assert "good" not in tables


def test_migrate_refuses_an_uninitialised_database() -> None:
    con = duckdb.connect(":memory:")
    with pytest.raises(MigrationError, match="not initialised"):
        migrate(con)


def test_init_refuses_a_database_of_unknown_provenance() -> None:
    """Tables but no watermark: guessing the state could corrupt it."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE somebody_elses_table (x INTEGER)")
    with pytest.raises(MigrationError, match="refusing to guess"):
        init_database(con)
