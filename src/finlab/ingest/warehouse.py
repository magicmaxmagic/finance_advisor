"""Writing hive-partitioned Parquet into the warehouse, atomically.

Ingestion never appends. A resumable run re-derives every partition it touches
from the immutable raw files, so appending would double-count on the second
run. Instead each partition is written to a staging directory and swapped into
place with `os.replace`, which is atomic within a filesystem: a reader either
sees the old partition or the new one, never a half-written directory.

The same writer serves `fact` (partitioned by taxonomy/filed_year) and
`price_daily` (partitioned by year), and is what M1's bulk loader will use
unchanged — only the source query differs.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import duckdb

from finlab.db.conn import sql_literal

# Hive directory components are built from data values, so they are constrained
# to characters that need no escaping and cannot escape the directory.
_SAFE_PARTITION_VALUE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class PartitionValueError(ValueError):
    """A partition value cannot be used as a directory name."""


@dataclass(frozen=True, slots=True)
class PartitionWrite:
    values: tuple[str, ...]
    path: Path
    rows: int


@dataclass(frozen=True, slots=True)
class WriteResult:
    partitions: tuple[PartitionWrite, ...]
    rows: int
    pruned: tuple[Path, ...]

    @property
    def partition_count(self) -> int:
        return len(self.partitions)


def _hive_component(column: str, value: object) -> str:
    text = str(value)
    if not _SAFE_PARTITION_VALUE.match(text):
        raise PartitionValueError(
            f"partition column {column!r} has value {text!r}, which is not safe as a directory name"
        )
    return f"{column}={text}"


def atomic_replace_dir(src: Path, dst: Path) -> None:
    """Move `src` onto `dst`, replacing it. Atomic within one filesystem.

    `os.replace` refuses a non-empty destination directory, so an existing
    partition is first moved aside, then deleted once the swap succeeds. If the
    swap fails the original is put back.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        src.replace(dst)
        return

    trash = dst.with_name(f".trash-{dst.name}-{uuid4().hex}")
    dst.replace(trash)
    try:
        src.replace(dst)
    except Exception:
        trash.replace(dst)
        raise
    shutil.rmtree(trash, ignore_errors=True)


def publish_table(
    con: duckdb.DuckDBPyConnection,
    *,
    source: str,
    root: Path,
    order_by: Sequence[str] = (),
    compression: str = "zstd",
) -> int:
    """Snapshot a small native table into the warehouse as one Parquet file.

    `entity`, `filing` and the other small tables live inside the DuckDB file,
    which only the writer may open. Readers — Streamlit, notebooks, CLI read
    commands — are forbidden from touching it (CLAUDE.md invariant 2) and would
    otherwise be unable to resolve a CIK to a company name. Publishing a
    read-model snapshot alongside the big partitioned datasets keeps the read
    path entirely on Parquet, so it works while the daemon holds the write lock.

    Unpartitioned: these tables are thousands of rows, not millions.
    """
    root = Path(root)
    staging = root.parent / f".staging-{root.name}-{uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        order = f" ORDER BY {', '.join(order_by)}" if order_by else ""
        con.execute(
            f"COPY (SELECT * FROM {source}{order}) "
            f"TO {sql_literal(str(staging / 'part-0.parquet'))} "
            f"(FORMAT PARQUET, COMPRESSION {compression})"
        )
        rows = int(con.execute(f"SELECT count(*) FROM {source}").fetchone()[0])
        atomic_replace_dir(staging, root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return rows


def write_partitioned(
    con: duckdb.DuckDBPyConnection,
    *,
    source: str,
    root: Path,
    partition_columns: Sequence[str],
    order_by: Sequence[str],
    compression: str = "zstd",
    prune_missing: bool = True,
) -> WriteResult:
    """Rewrite every partition present in `source`, then swap them into `root`.

    `source` is a SQL relation — a table name, or a parenthesised subquery.
    Rows are sorted by `order_by` within each file so DuckDB's row-group
    statistics prune hard on single-company queries.

    With `prune_missing`, partitions under `root` that the source no longer
    produces are deleted. That is correct here because a run always rebuilds
    from the complete set of raw files, not from a delta.
    """
    if not partition_columns:
        raise ValueError("partition_columns must not be empty")

    root = Path(root)
    partition_list = ", ".join(partition_columns)
    rows_seen = 0

    partitions = con.execute(
        f"SELECT {partition_list}, count(*) AS n FROM {source} "
        f"GROUP BY {partition_list} ORDER BY {partition_list}"
    ).fetchall()

    staging = root.parent / f".staging-{root.name}-{uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=True)
    written: list[PartitionWrite] = []

    try:
        for row in partitions:
            values = tuple(str(v) for v in row[:-1])
            count = int(row[-1])
            rows_seen += count

            relative = Path(
                *(
                    _hive_component(col, val)
                    for col, val in zip(partition_columns, values, strict=True)
                )
            )
            target_dir = staging / relative
            target_dir.mkdir(parents=True, exist_ok=True)

            predicate = " AND ".join(
                f"{col} = {sql_literal(val)}"
                for col, val in zip(partition_columns, values, strict=True)
            )
            file_path = target_dir / "part-0.parquet"
            con.execute(
                f"COPY (SELECT * EXCLUDE ({partition_list}) FROM {source} "
                f"WHERE {predicate} ORDER BY {', '.join(order_by)}) "
                f"TO {sql_literal(str(file_path))} "
                f"(FORMAT PARQUET, COMPRESSION {compression})"
            )
            written.append(PartitionWrite(values=values, path=root / relative, rows=count))

        for partition in written:
            source_dir = staging / partition.path.relative_to(root)
            atomic_replace_dir(source_dir, partition.path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    pruned: list[Path] = []
    if prune_missing:
        keep = {p.path.resolve() for p in written}
        depth = len(partition_columns)
        for existing in sorted(root.glob("/".join(["*"] * depth))):
            if existing.is_dir() and existing.resolve() not in keep:
                shutil.rmtree(existing, ignore_errors=True)
                pruned.append(existing)
        # Removing a leaf can strand its ancestors: dropping every 2025 partition
        # of a taxonomy leaves an empty `taxonomy=.../` behind, which reads as a
        # partition that exists but has no data.
        for level in range(depth - 1, 0, -1):
            for directory in sorted(root.glob("/".join(["*"] * level)), reverse=True):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()

    return WriteResult(partitions=tuple(written), rows=rows_seen, pruned=tuple(pruned))
