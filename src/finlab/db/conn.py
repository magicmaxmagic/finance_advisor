"""DuckDB connection management.

Invariant 2 of CLAUDE.md: exactly one process holds the read-write connection to
`data/db/finlab.duckdb`. Everyone else reads the Parquet warehouse through an
in-memory database.

Three entry points, in order of how often you should reach for them:

- `read_conn()`      in-memory DuckDB with views over the Parquet warehouse.
                     The default for notebooks, Analytics and CLI read commands.
- `write_conn()`     the single writer, guarded by an OS-level advisory lock so
                     a second one fails loudly instead of corrupting anything.
- `open_db_read_only()`  reads the DuckDB file itself. Administrative use only
                     (`finlab db status`); fails while a writer holds the file.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from finlab.config import Settings, get_settings

# Datasets published to the Parquet warehouse and exposed to readers as views.
#
# `fact` and `price_daily` live there because they are too large for the DuckDB
# file (architecture §1). The rest are small tables that live natively in the
# database and are *also* snapshotted to Parquet, because readers are forbidden
# from opening the DuckDB file (invariant 2) and would otherwise have no way to
# resolve a CIK to a name or a concept to its tags.
WAREHOUSE_DATASETS: tuple[str, ...] = (
    "fact",
    "price_daily",
    "entity",
    "filing",
    "concept_map",
    "security",
    "metric",
    "fundamentals_q4",
    "universe_snapshot",
    "corporate_action",
    "price_ingestion",
    "ticker_history",
    "share_class_fact",
    "financial_event",
)


class WriterLockHeld(RuntimeError):
    """Another process already holds the read-write connection."""


class DatabaseMissing(FileNotFoundError):
    """The DuckDB file does not exist yet. Run `finlab db init`."""


def sql_literal(value: str) -> str:
    """Quote a string for interpolation into DDL that cannot take a bind parameter."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _apply_pragmas(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    con.execute("SET TimeZone = 'UTC'")
    con.execute(f"SET memory_limit={sql_literal(settings.duckdb_memory_limit)}")
    con.execute(f"SET threads={settings.duckdb_threads:d}")


def load_extensions(con: duckdb.DuckDBPyConnection, *names: str) -> None:
    """Install and load DuckDB extensions.

    Not called during `db init`: the first install of an extension needs
    network access, and initialising a database must work offline. Ingestion
    (`httpfs`) and vector search (`vss`) call this when they actually need it.
    """
    for name in names:
        con.execute(f"INSTALL {name}")
        con.execute(f"LOAD {name}")


@contextmanager
def _writer_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = os.read(fd, 64).decode(errors="replace").strip() or "unknown"
            raise WriterLockHeld(
                f"another process holds the finlab write lock ({lock_path}, "
                f"pid {holder}). Exactly one writer is allowed — stop finlabd "
                f"before running write commands."
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        # Closing the descriptor releases the flock.
        os.close(fd)


@contextmanager
def write_conn(settings: Settings | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
    """The single read-write connection. Creates the database file if absent.

    Raises `WriterLockHeld` if another process (normally `finlabd`) already has
    it. The lock is advisory but process-wide: opening a second writer inside
    one process fails too, which is deliberate.
    """
    cfg = settings or get_settings()
    cfg.db_dir.mkdir(parents=True, exist_ok=True)
    with _writer_lock(Path(f"{cfg.db_path}.lock")):
        recover_working_directories(cfg.warehouse_dir)
        con = duckdb.connect(str(cfg.db_path))
        try:
            _apply_pragmas(con, cfg)
            yield con
        finally:
            con.close()


def recover_working_directories(warehouse_dir: Path) -> None:
    """Restore a dataset interrupted between renames, under the writer lock.

    A present destination is already committed. Leave obsolete backups alone:
    readers exclude hidden paths and recovery must never guess at deleting data.
    """
    for dataset in WAREHOUSE_DATASETS:
        destination = warehouse_dir / dataset
        if destination.exists():
            continue
        backups = [p for p in warehouse_dir.glob(f".trash-{dataset}-*") if p.is_dir()]
        if backups:
            max(backups, key=lambda p: p.stat().st_mtime_ns).replace(destination)


def parquet_files(root: Path) -> list[Path]:
    """Committed files only; interrupted publication trash is never a dataset."""
    return sorted(
        path
        for path in root.rglob("*.parquet")
        if not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def attach_warehouse_views(
    con: duckdb.DuckDBPyConnection,
    settings: Settings | None = None,
    *,
    warehouse_dir: Path | None = None,
) -> list[str]:
    """Expose the Parquet warehouse as views. Returns the names actually attached.

    A dataset with no Parquet files yet is skipped rather than attached as an
    empty view, so a reader can tell "not ingested" from "ingested, no rows".
    """
    cfg = settings or get_settings()
    attached: list[str] = []
    for dataset in WAREHOUSE_DATASETS:
        root = (warehouse_dir or cfg.warehouse_dir) / dataset
        files = parquet_files(root)
        if not root.is_dir() or not files:
            continue
        # Hive columns are reconstructed from `key=value` directories; a flat
        # snapshot has none, and asking for them there is a needless mismatch.
        hive = any(child.is_dir() and "=" in child.name for child in root.iterdir())
        glob = "[" + ", ".join(sql_literal(str(path)) for path in files) + "]"
        con.execute(
            f"CREATE OR REPLACE VIEW {dataset} AS "
            f"SELECT * FROM read_parquet({glob}, hive_partitioning = {str(hive).lower()}, union_by_name = true)"
        )
        attached.append(dataset)
    return attached


def new_read_conn(
    settings: Settings | None = None, *, attach_warehouse: bool = True
) -> duckdb.DuckDBPyConnection:
    """An open in-memory database over the Parquet warehouse. Caller closes it.

    Long-lived readers — such as Analytics services — need a
    connection they own rather than a context manager.
    """
    cfg = settings or get_settings()
    con = duckdb.connect(":memory:")
    _apply_pragmas(con, cfg)
    if attach_warehouse:
        attach_warehouse_views(con, cfg)
    return con


@contextmanager
def read_conn(
    settings: Settings | None = None, *, attach_warehouse: bool = True
) -> Iterator[duckdb.DuckDBPyConnection]:
    """An in-memory database with views over the Parquet warehouse.

    This is what every reader should use. It never touches the DuckDB file, so
    it cannot contend with the writer.
    """
    con = new_read_conn(settings, attach_warehouse=attach_warehouse)
    try:
        yield con
    finally:
        con.close()


@contextmanager
def open_db_read_only(
    settings: Settings | None = None,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Read the DuckDB file itself, read-only.

    For administrative commands that need the native tables (`finlab db
    status`). DuckDB will refuse this while a writer holds the file, which is
    expected — stop the daemon, or read the warehouse instead.
    """
    cfg = settings or get_settings()
    if not cfg.db_path.exists():
        raise DatabaseMissing(f"{cfg.db_path} does not exist. Run `finlab db init` first.")
    con = duckdb.connect(str(cfg.db_path), read_only=True)
    try:
        _apply_pragmas(con, cfg)
        yield con
    finally:
        con.close()
