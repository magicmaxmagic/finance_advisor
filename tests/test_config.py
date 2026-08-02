from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from finlab.config import Settings


def _settings(tmp_path: Path, **kwargs: object) -> Settings:
    return Settings(data_dir=tmp_path / "data", _env_file=None, **kwargs)


def test_paths_derive_from_data_dir(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    root = tmp_path / "data"
    assert cfg.raw_dir == root / "raw"
    assert cfg.warehouse_dir == root / "warehouse"
    assert cfg.db_path == root / "db" / "finlab.duckdb"
    assert cfg.reports_dir == root / "reports"
    assert cfg.mlruns_dir == root / "mlruns"


def test_data_dir_is_resolved_absolute(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    assert cfg.data_dir.is_absolute()


def test_ensure_dirs_is_idempotent(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    assert cfg.missing_dirs()
    cfg.ensure_dirs()
    cfg.ensure_dirs()
    assert cfg.missing_dirs() == []


def test_missing_parent_directory_is_rejected(tmp_path: Path) -> None:
    """A typo'd data dir must fail loudly, not scatter directories."""
    with pytest.raises(ValidationError, match="does not exist"):
        Settings(data_dir=tmp_path / "no" / "such" / "parent" / "data", _env_file=None)


def test_user_agent_must_carry_a_contact_address(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="contact email"):
        _settings(tmp_path, sec_user_agent="finlab")


def test_placeholder_user_agent_is_detected(tmp_path: Path) -> None:
    assert _settings(tmp_path).sec_user_agent_is_placeholder
    assert not _settings(
        tmp_path, sec_user_agent="finlab jane@acme.com"
    ).sec_user_agent_is_placeholder


def test_sec_rate_limit_cannot_exceed_the_published_ceiling(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _settings(tmp_path, sec_max_rps=25)


def test_ollama_concurrency_cannot_exceed_the_tier_limit(tmp_path: Path) -> None:
    """Overshooting 3 yields rejections, not throughput."""
    with pytest.raises(ValidationError):
        _settings(tmp_path, ollama_max_concurrency=8)


@pytest.mark.parametrize("value", ["9GB", "512MB", "1.5GiB"])
def test_memory_limit_accepts_duckdb_size_literals(tmp_path: Path, value: str) -> None:
    assert _settings(tmp_path, duckdb_memory_limit=value).duckdb_memory_limit == value


@pytest.mark.parametrize("value", ["lots", "9 gigabytes", "'; DROP TABLE fact; --"])
def test_memory_limit_rejects_junk(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValidationError, match="size literal"):
        _settings(tmp_path, duckdb_memory_limit=value)


def test_env_vars_are_read_with_the_finlab_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FINLAB_DATA_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("FINLAB_SEC_USER_AGENT", "finlab jane@acme.com")
    cfg = Settings(_env_file=None)
    assert cfg.data_dir == (tmp_path / "elsewhere").resolve()
    assert cfg.sec_user_agent == "finlab jane@acme.com"
