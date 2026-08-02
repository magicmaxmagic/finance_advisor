"""Forward-only migration runner.

No Alembic, no ORM (CLAUDE.md). Numbered `.sql` files and a watermark table.

Two paths into a correct schema:

- **Fresh database** — `schema.sql` is applied whole, then `schema_version` is
  stamped with the *highest* migration number on disk, because `schema.sql` is
  maintained to already include every migration's effect. A fresh install never
  replays history.
- **Existing database** — only migrations numbered above the current watermark
  are applied, in order.

`schema_version.version` is therefore a watermark, not a set of applied files.

Adding a schema change means doing both halves: write
`migrations/NNNN_short_name.sql` *and* update `schema.sql`. Migrations must not
contain `BEGIN`/`COMMIT`; the runner wraps each one in a transaction.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

DB_DIR = Path(__file__).parent
SCHEMA_PATH = DB_DIR / "schema.sql"
MIGRATIONS_DIR = DB_DIR / "migrations"

BASELINE_NAME = "baseline (schema.sql)"

_MIGRATION_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    """A migration file is malformed, misnumbered, or failed to apply."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path

    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class InitResult:
    """What `init_database` did."""

    baseline_applied: bool
    migrations_applied: tuple[Migration, ...]
    version: int


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Every migration on disk, ordered by version.

    Files that do not match `NNNN_lower_snake_case.sql` are an error rather than
    something to skip quietly — a typo'd filename would otherwise silently never
    run.
    """
    # Resolved at call time, not bound as a default, so the module attribute can
    # be redirected in tests.
    directory = MIGRATIONS_DIR if directory is None else directory
    if not directory.is_dir():
        return []

    migrations: list[Migration] = []
    seen: dict[int, Path] = {}
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or path.suffix != ".sql":
            continue
        match = _MIGRATION_FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"{path.name}: migration files must be named "
                f"NNNN_lower_snake_case.sql, e.g. 0001_add_split_factor.sql"
            )
        version = int(match.group(1))
        if version in seen:
            raise MigrationError(
                f"duplicate migration number {version:04d}: {seen[version].name} and {path.name}"
            )
        seen[version] = path
        migrations.append(Migration(version=version, name=match.group(2), path=path))
    return migrations


def _schema_version_exists(con: duckdb.DuckDBPyConnection) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = 'schema_version'"
    ).fetchone()
    return bool(row and row[0])


def _user_table_count(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name <> 'schema_version'"
    ).fetchone()
    return int(row[0]) if row else 0


def current_version(con: duckdb.DuckDBPyConnection) -> int:
    """The watermark, or -1 if the database has not been initialised."""
    if not _schema_version_exists(con):
        return -1
    row = con.execute("SELECT coalesce(max(version), -1) FROM schema_version").fetchone()
    return int(row[0]) if row else -1


def _stamp(con: duckdb.DuckDBPyConnection, version: int, name: str, checksum: str) -> None:
    con.execute(
        "INSERT INTO schema_version (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
        [version, name, checksum, datetime.now(UTC).replace(tzinfo=None)],
    )


def _apply_and_stamp(
    con: duckdb.DuckDBPyConnection, sql: str, version: int, name: str, label: str
) -> None:
    """Run one SQL script and record it, atomically."""
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(sql)
        _stamp(con, version, name, _checksum(sql))
    except Exception as exc:
        con.execute("ROLLBACK")
        raise MigrationError(f"{label} failed to apply: {exc}") from exc
    con.execute("COMMIT")


def apply_baseline(con: duckdb.DuckDBPyConnection, *, schema_path: Path = SCHEMA_PATH) -> int:
    """Apply `schema.sql` to an empty database and stamp the watermark.

    Returns the version stamped: the highest migration number on disk, since
    `schema.sql` already reflects all of them.
    """
    sql = schema_path.read_text(encoding="utf-8")
    version = max((m.version for m in discover_migrations()), default=0)
    _apply_and_stamp(con, sql, version, BASELINE_NAME, schema_path.name)
    return version


def migrate(con: duckdb.DuckDBPyConnection) -> list[Migration]:
    """Apply every migration above the watermark, in order. Returns what ran."""
    version = current_version(con)
    if version < 0:
        raise MigrationError("database is not initialised — run `finlab db init` first")

    applied: list[Migration] = []
    for migration in (m for m in discover_migrations() if m.version > version):
        _apply_and_stamp(
            con, migration.sql(), migration.version, migration.name, migration.path.name
        )
        applied.append(migration)
    return applied


def init_database(con: duckdb.DuckDBPyConnection) -> InitResult:
    """Bring a database up to the current schema, fresh or existing. Idempotent."""
    if not _schema_version_exists(con):
        if _user_table_count(con) > 0:
            raise MigrationError(
                "database contains tables but no schema_version — refusing to "
                "guess its state. Inspect it, or start from an empty file."
            )
        version = apply_baseline(con)
        return InitResult(baseline_applied=True, migrations_applied=(), version=version)

    applied = migrate(con)
    return InitResult(
        baseline_applied=False,
        migrations_applied=tuple(applied),
        version=current_version(con),
    )
