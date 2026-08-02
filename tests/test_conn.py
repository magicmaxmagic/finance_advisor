"""Invariant 2: exactly one read-write connection."""

from __future__ import annotations

import pytest

from finlab.config import Settings
from finlab.db.conn import (
    DatabaseMissing,
    WriterLockHeld,
    attach_warehouse_views,
    open_db_read_only,
    read_conn,
    write_conn,
)


def test_write_conn_creates_the_database_file(settings: Settings) -> None:
    settings.ensure_dirs()
    assert not settings.db_path.exists()
    with write_conn(settings) as con:
        con.execute("CREATE TABLE t (a INTEGER)")
    assert settings.db_path.exists()


def test_second_writer_is_refused(settings: Settings) -> None:
    """The lock is per open-file-description, so this catches an accidental
    second writer inside one process too, not just a competing daemon."""
    settings.ensure_dirs()
    with write_conn(settings), pytest.raises(WriterLockHeld, match="one writer"):
        with write_conn(settings):
            pass


def test_the_lock_is_released_after_use(settings: Settings) -> None:
    settings.ensure_dirs()
    with write_conn(settings):
        pass
    with write_conn(settings) as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1


def test_the_lock_is_released_when_the_body_raises(settings: Settings) -> None:
    settings.ensure_dirs()
    with pytest.raises(RuntimeError, match="boom"):
        with write_conn(settings):
            raise RuntimeError("boom")
    with write_conn(settings) as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1


def test_read_conn_is_in_memory_and_touches_no_file(settings: Settings) -> None:
    settings.ensure_dirs()
    with read_conn(settings) as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1
    assert not settings.db_path.exists()


def test_readers_do_not_contend_with_the_writer(settings: Settings) -> None:
    """A reader must work while the daemon holds the write connection."""
    settings.ensure_dirs()
    with write_conn(settings) as writer:
        writer.execute("CREATE TABLE t (a INTEGER)")
        with read_conn(settings) as reader:
            assert reader.execute("SELECT 42").fetchone()[0] == 42


def test_warehouse_views_are_absent_until_parquet_exists(settings: Settings) -> None:
    settings.ensure_dirs()
    with read_conn(settings) as con:
        assert attach_warehouse_views(con, settings) == []


def test_warehouse_views_appear_once_parquet_is_written(settings: Settings) -> None:
    settings.ensure_dirs()
    partition = settings.warehouse_dir / "fact" / "taxonomy=us-gaap" / "filed_year=2025"
    partition.mkdir(parents=True)

    with read_conn(settings, attach_warehouse=False) as con:
        con.execute(
            "COPY (SELECT 320193 AS cik, 'Revenues' AS tag, 1.0 AS value) "
            f"TO '{partition / 'part-0.parquet'}' (FORMAT PARQUET)"
        )

    with read_conn(settings) as con:
        assert attach_warehouse_views(con, settings) == ["fact"]
        assert con.execute("SELECT count(*) FROM fact").fetchone()[0] == 1
        # hive partitioning must surface the partition columns
        assert con.execute("SELECT taxonomy FROM fact").fetchone()[0] == "us-gaap"


def test_open_db_read_only_reports_a_missing_database(settings: Settings) -> None:
    settings.ensure_dirs()
    with pytest.raises(DatabaseMissing, match="finlab db init"):
        with open_db_read_only(settings):
            pass


def _bytes_from_duckdb_size(text: str) -> float:
    """DuckDB echoes memory_limit in binary units ('8.3 GiB' for a 9GB setting)."""
    number, unit = text.split()
    multiplier = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    return float(number) * multiplier[unit]


def test_memory_limit_and_threads_are_applied(settings: Settings) -> None:
    """The 9 GB ceiling is what keeps a cross-sectional join off the swap file."""
    with read_conn(settings) as con:
        limit = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
        threads = con.execute("SELECT current_setting('threads')").fetchone()[0]
    assert _bytes_from_duckdb_size(limit) == pytest.approx(9 * 1000**3, rel=0.02)
    assert int(threads) == settings.duckdb_threads
