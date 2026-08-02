from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from finlab.cli import app
from finlab.config import get_settings

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "data"
    monkeypatch.setenv("FINLAB_DATA_DIR", str(target))
    get_settings.cache_clear()
    return target


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "finlab" in result.stdout


def test_db_init_creates_the_data_tree_and_database(data_dir: Path) -> None:
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0, result.stdout
    assert (data_dir / "db" / "finlab.duckdb").exists()
    for sub in ("raw", "staging", "warehouse", "db", "reports", "mlruns"):
        assert (data_dir / sub).is_dir()


def test_db_init_is_idempotent(data_dir: Path) -> None:
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    second = runner.invoke(app, ["db", "init"])
    assert second.exit_code == 0
    assert "up to date" in second.stdout


def test_db_status_lists_tables_with_zero_rows(data_dir: Path) -> None:
    runner.invoke(app, ["db", "init"])
    result = runner.invoke(app, ["db", "status"])
    assert result.exit_code == 0, result.stdout
    for name in ("fact", "filing", "entity", "concept_map", "job"):
        assert name in result.stdout
    assert "schema version" in result.stdout


def test_db_status_without_a_database_fails_with_guidance(data_dir: Path) -> None:
    result = runner.invoke(app, ["db", "status"])
    assert result.exit_code == 1
    assert "finlab db init" in result.output


def test_db_migrate_reports_nothing_pending(data_dir: Path) -> None:
    runner.invoke(app, ["db", "init"])
    result = runner.invoke(app, ["db", "migrate"])
    assert result.exit_code == 0
    assert "no pending migrations" in result.stdout
