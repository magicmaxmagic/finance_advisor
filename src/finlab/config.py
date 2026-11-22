"""Settings, loaded from the environment and `.env`.

Every setting is prefixed `FINLAB_`. See `.env.example` for the full list.
Paths are all derived from a single `data_dir` so that relocating the data tree
is a one-line change.
"""

from __future__ import annotations

import math
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Shipped in .env.example. Network code must refuse to run while this is unchanged:
# the SEC blocks traffic that does not identify a real contact address.
PLACEHOLDER_USER_AGENT_DOMAIN = "you@example.com"

# Validated here rather than at query time, because the value is interpolated
# into a DuckDB `SET` statement.
_MEMORY_LIMIT_RE = re.compile(r"^\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$", re.IGNORECASE)


class Settings(BaseSettings):
    """Runtime configuration for every finlab process."""

    model_config = SettingsConfigDict(
        env_prefix="FINLAB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- paths ---------------------------------------------------------------
    data_dir: Path = Path("./data")
    universe_path: Path | None = Path(__file__).resolve().parent / "data/major_us_universe.yaml"
    factor_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "value": 0.2,
            "quality": 0.2,
            "growth": 0.2,
            "momentum": 0.2,
            "risk": 0.2,
        }
    )

    # --- SEC -----------------------------------------------------------------
    sec_user_agent: str = f"finlab {PLACEHOLDER_USER_AGENT_DOMAIN}"
    # SEC's published ceiling is 10 req/s. The field caps it so no caller can
    # raise it past the limit by editing a config file.
    sec_max_rps: float = Field(default=10.0, gt=0, le=10.0)
    sec_max_concurrency: int = Field(default=8, ge=1, le=16)

    # Incremental Yahoo recovery; overlap is calendar days, not trading sessions.
    price_history_start: date = date(2006, 1, 1)
    price_correction_overlap_days: int = Field(default=10, ge=1, le=365)

    # --- market data (IEX, read-only requests) ------------------------------
    alpaca_key_id: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    market_mode: Literal["demo", "live"] = "demo"

    # --- Ollama Cloud --------------------------------------------------------
    ollama_base_url: str = "https://ollama.com"
    ollama_api_key: str = ""
    # Pro tier allows exactly 3 concurrent models. Overshooting yields
    # rejections, not throughput, so the ceiling is enforced here.
    ollama_max_concurrency: int = Field(default=3, ge=1, le=3)

    # --- DuckDB --------------------------------------------------------------
    # 9 GB on a 16 GB host leaves room for the OS and the Python process.
    duckdb_memory_limit: str = "9GB"
    duckdb_threads: int = Field(default=4, ge=1)

    # --- logging -------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    @field_validator("factor_weights")
    @classmethod
    def _validate_factor_weights(cls, weights):
        if (
            set(weights) != {"value", "quality", "growth", "momentum", "risk"}
            or any(not math.isfinite(w) or w < 0 for w in weights.values())
            or abs(sum(weights.values()) - 1) > 1e-9
        ):
            raise ValueError(
                "Provide finite nonnegative weights for all five families summing to one"
            )
        return weights

    @field_validator("duckdb_memory_limit")
    @classmethod
    def _memory_limit_is_well_formed(cls, v: str) -> str:
        if not _MEMORY_LIMIT_RE.match(v.strip()):
            raise ValueError(
                f"FINLAB_DUCKDB_MEMORY_LIMIT={v!r} is not a DuckDB size literal, "
                "e.g. '9GB' or '512MB'."
            )
        return v.strip()

    @field_validator("sec_user_agent")
    @classmethod
    def _user_agent_carries_contact(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError(
                "FINLAB_SEC_USER_AGENT must carry a contact email address, "
                "e.g. 'finlab jane@example.com'. The SEC blocks anonymous traffic."
            )
        return v

    @model_validator(mode="after")
    def _resolve_data_dir(self) -> Settings:
        resolved = self.data_dir.expanduser().resolve()
        # The tree itself is created by ensure_dirs(); its parent must already
        # exist, so that a typo'd path fails loudly instead of silently
        # scattering directories somewhere unexpected.
        if not resolved.parent.is_dir():
            raise ValueError(
                f"FINLAB_DATA_DIR={self.data_dir} — parent directory "
                f"{resolved.parent} does not exist."
            )
        object.__setattr__(self, "data_dir", resolved)
        return self

    # --- derived paths -------------------------------------------------------

    @computed_field  # type: ignore[prop-decorator]
    @property
    def raw_dir(self) -> Path:
        """Immutable downloads, sha256-addressed."""
        return self.data_dir / "raw"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def staging_dir(self) -> Path:
        """Transient parquet. Safe to delete at any time."""
        return self.data_dir / "staging"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def warehouse_dir(self) -> Path:
        """Hive-partitioned parquet: the lake."""
        return self.data_dir / "warehouse"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_dir(self) -> Path:
        return self.data_dir / "db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_path(self) -> Path:
        """The single DuckDB file. Exactly one process may open it read-write."""
        return self.db_dir / "finlab.duckdb"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mlruns_dir(self) -> Path:
        """MLflow file backend."""
        return self.data_dir / "mlruns"

    @property
    def sec_user_agent_is_placeholder(self) -> bool:
        """True while the shipped example contact address is still in place."""
        return PLACEHOLDER_USER_AGENT_DOMAIN in self.sec_user_agent

    # --- filesystem ----------------------------------------------------------

    def data_subdirs(self) -> tuple[Path, ...]:
        return (
            self.raw_dir,
            self.staging_dir,
            self.warehouse_dir,
            self.db_dir,
            self.reports_dir,
            self.mlruns_dir,
        )

    def ensure_dirs(self) -> None:
        """Create the data tree. Idempotent."""
        for path in self.data_subdirs():
            path.mkdir(parents=True, exist_ok=True)

    def missing_dirs(self) -> list[Path]:
        return [p for p in self.data_subdirs() if not p.is_dir()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached; call `get_settings.cache_clear()` in tests."""
    return Settings()
