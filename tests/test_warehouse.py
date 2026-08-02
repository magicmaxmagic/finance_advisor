"""Atomic partition writing."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from finlab.ingest.warehouse import (
    PartitionValueError,
    atomic_replace_dir,
    publish_table,
    write_partitioned,
)


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE src AS SELECT * FROM (VALUES
          (1, 'us-gaap', 2024, 10.0),
          (2, 'us-gaap', 2024, 20.0),
          (3, 'us-gaap', 2025, 30.0),
          (4, 'dei',     2025, 40.0)
        ) t(cik, taxonomy, filed_year, value)
        """
    )
    return connection


def test_atomic_replace_over_an_existing_directory(tmp_path: Path) -> None:
    src, dst = tmp_path / "new", tmp_path / "live"
    src.mkdir()
    (src / "part-0.parquet").write_text("new")
    dst.mkdir()
    (dst / "part-0.parquet").write_text("old")
    (dst / "stale.parquet").write_text("stale")

    atomic_replace_dir(src, dst)

    assert (dst / "part-0.parquet").read_text() == "new"
    assert not (dst / "stale.parquet").exists(), "replacement must not merge with the old contents"
    assert not src.exists()
    assert not list(tmp_path.glob(".trash-*")), "trash must be cleaned up"


def test_atomic_replace_when_the_destination_is_absent(tmp_path: Path) -> None:
    src = tmp_path / "new"
    src.mkdir()
    (src / "part-0.parquet").write_text("new")
    atomic_replace_dir(src, tmp_path / "nested" / "live")
    assert (tmp_path / "nested" / "live" / "part-0.parquet").read_text() == "new"


def test_partitions_are_written_as_hive_directories(
    con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    root = tmp_path / "fact"
    result = write_partitioned(
        con,
        source="src",
        root=root,
        partition_columns=("taxonomy", "filed_year"),
        order_by=("cik",),
    )
    assert result.rows == 4
    assert result.partition_count == 3
    assert (root / "taxonomy=us-gaap" / "filed_year=2024" / "part-0.parquet").is_file()
    assert (root / "taxonomy=dei" / "filed_year=2025" / "part-0.parquet").is_file()


def test_written_data_round_trips_with_partition_columns(
    con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    root = tmp_path / "fact"
    write_partitioned(
        con,
        source="src",
        root=root,
        partition_columns=("taxonomy", "filed_year"),
        order_by=("cik",),
    )
    rows = con.execute(
        f"SELECT cik, taxonomy, filed_year, value "
        f"FROM read_parquet('{root}/**/*.parquet', hive_partitioning = true) ORDER BY cik"
    ).fetchall()
    assert rows == [
        (1, "us-gaap", 2024, 10.0),
        (2, "us-gaap", 2024, 20.0),
        (3, "us-gaap", 2025, 30.0),
        (4, "dei", 2025, 40.0),
    ]


def test_rewriting_replaces_rather_than_appends(
    con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """The resume manifest means partitions get rebuilt; appending would double."""
    root = tmp_path / "fact"
    kwargs = {
        "root": root,
        "partition_columns": ("taxonomy", "filed_year"),
        "order_by": ("cik",),
    }
    write_partitioned(con, source="src", **kwargs)
    write_partitioned(con, source="src", **kwargs)

    total = con.execute(
        f"SELECT count(*) FROM read_parquet('{root}/**/*.parquet', hive_partitioning = true)"
    ).fetchone()[0]
    assert total == 4


def test_partitions_no_longer_present_are_pruned(
    con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    root = tmp_path / "fact"
    kwargs = {
        "root": root,
        "partition_columns": ("taxonomy", "filed_year"),
        "order_by": ("cik",),
    }
    write_partitioned(con, source="src", **kwargs)
    assert (root / "taxonomy=dei" / "filed_year=2025").is_dir()

    con.execute("DELETE FROM src WHERE taxonomy = 'dei'")
    result = write_partitioned(con, source="src", **kwargs)

    assert not (root / "taxonomy=dei").exists()
    assert len(result.pruned) == 1


def test_unsafe_partition_values_are_refused(
    con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    con.execute("INSERT INTO src VALUES (9, '../escape', 2025, 1.0)")
    with pytest.raises(PartitionValueError, match="not safe"):
        write_partitioned(
            con,
            source="src",
            root=tmp_path / "fact",
            partition_columns=("taxonomy", "filed_year"),
            order_by=("cik",),
        )


def test_publish_table_snapshots_a_small_table(
    con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    root = tmp_path / "entity"
    assert publish_table(con, source="src", root=root, order_by=("cik",)) == 4
    assert (root / "part-0.parquet").is_file()
    assert con.execute(f"SELECT count(*) FROM read_parquet('{root}/*.parquet')").fetchone()[0] == 4
