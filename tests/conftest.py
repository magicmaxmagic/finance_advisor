"""Shared fixtures.

Every test runs against a throwaway data tree under `tmp_path`, and with the
repository's own `.env` and any inherited `FINLAB_*` variables ignored, so
results never depend on the developer's local configuration.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from finlab.config import Settings, get_settings
from finlab.db.conn import write_conn
from finlab.db.migrations import init_database


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in [k for k in os.environ if k.startswith("FINLAB_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "FINLAB_UNIVERSE_PATH",
        str(Path(__file__).resolve().parents[1] / "src/finlab/data/poc_universe.yaml"),
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at an empty data tree, ignoring the repo's `.env`."""
    return Settings(data_dir=tmp_path / "data", _env_file=None)


@pytest.fixture
def initialised(settings: Settings) -> Settings:
    """A settings object whose database has been created and migrated."""
    settings.ensure_dirs()
    with write_conn(settings) as con:
        init_database(con)
    return settings
