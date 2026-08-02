"""DuckDB connection management.

Invariant 2 of CLAUDE.md: exactly one process holds the read-write connection to
`data/db/finlab.duckdb`. Everyone else reads the Parquet warehouse through an
in-memory database.

Three entry points, in order of how often you should reach for them:

- `read_conn()`      in-memory DuckDB with views over the Parquet warehouse.
                     The default for notebooks, Streamlit and CLI read commands.
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

# Tables that live as hive-partitioned Parquet rather than inside the DuckDB
# file, and are therefore exposed to readers as views. See architecture §1.
WAREHOUSE_DATASETS: tuple[str, ...] = ("fact", "price_daily")


class WriterLockHeld(RuntimeError):
    """Another process already holds the read-write connection."""


class DatabaseMissing(FileNotFoundError):
    """The DuckDB file does not exist yet. Run `finlab db init`."""


def _sql_literal(value: str) -> str:
    """Quote a string for interpolation into DDL that cannot take a bind parameter."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _apply_pragmas(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    con.execute(f"SET memory_limit={_sql_literal(settings.duckdb_memory_limit)}")
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
        con = duckdb.connect(str(cfg.db_path))
        try:
            _apply_pragmas(con, cfg)
            yield con
        finally:
            con.close()


def attach_warehouse_views(
    con: duckdb.DuckDBPyConnection, settings: Settings | None = None
) -> list[str]:
    """Expose the Parquet warehouse as views. Returns the names actually attached.

    A dataset with no Parquet files yet is skipped rather than attached as an
    empty view, so a reader can tell "not ingested" from "ingested, no rows".
    """
    cfg = settings or get_settings()
    attached: list[str] = []
    for dataset in WAREHOUSE_DATASETS:
        root = cfg.warehouse_dir / dataset
        if not root.is_dir() or not any(root.rglob("*.parquet")):
            continue
        glob = _sql_literal(f"{root}/**/*.parquet")
        con.execute(
            f"CREATE OR REPLACE VIEW {dataset} AS "
            f"SELECT * FROM read_parquet({glob}, hive_partitioning = true)"
        )
        attached.append(dataset)
    return attached


@contextmanager
def read_conn(
    settings: Settings | None = None, *, attach_warehouse: bool = True
) -> Iterator[duckdb.DuckDBPyConnection]:
    """An in-memory database with views over the Parquet warehouse.

    This is what every reader should use. It never touches the DuckDB file, so
    it cannot contend with the writer.
    """
    cfg = settings or get_settings()
    con = duckdb.connect(":memory:")
    try:
        _apply_pragmas(con, cfg)
        if attach_warehouse:
            attach_warehouse_views(con, cfg)
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
