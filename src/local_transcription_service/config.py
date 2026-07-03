"""Service configuration loaded from LTS_* environment variables.

All values come from env (via pydantic-settings). Defaults match
HLD-001 Sections 4, 7, 14. See HLD-001 for the rationale behind
each default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_DATA_DIR = Path.home() / ".local-transcription"


class Settings(BaseSettings):
    """Service configuration.

    All values are loaded from `LTS_*` environment variables. The
    `.env` file is intentionally not loaded — production env comes
    from the launchd plist, test env from pytest fixtures.

    Env-var names follow HLD-001 (the HLD is the source of truth
    for the operator-facing contract). Two fields override the
    auto-derived `LTS_<FIELD>` mapping with an explicit alias so
    the HLD-mandated names line up:

    - `bind_port` is read from `LTS_PORT` (HLD-001 §14).
    - `stt_model` is read from `LTS_MODEL` (HLD-001 §4).

    The Python field names are kept as `bind_port` / `stt_model`
    because HLD-001 §15 logs them as such in the `config_resolved`
    startup event.
    """

    model_config = SettingsConfigDict(
        env_prefix="LTS_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    # --- Network binding (HLD-001 §14) ---
    bind_host: str = "192.168.0.99"
    bind_port: int = Field(default=8766, validation_alias="LTS_PORT")

    # --- Auth (HLD-001 §14) ---
    # Required. No default. Minimum 16 chars to prevent trivial tokens.
    auth_token: str = Field(..., min_length=16)

    # --- Data directory layout (HLD-001 §7, §13) ---
    data_dir: Path = _DEFAULT_DATA_DIR

    # --- Queue / lease (HLD-001 §8, §10) ---
    lease_ttl_seconds: int = 600
    reclaim_interval_seconds: int = 30
    max_attempts: int = 2
    retry_backoff_seconds: int = 30

    # --- STT engine (HLD-001 §4) ---
    stt_engine: Literal["ollama", "mlx-whisper", "mock"] = "ollama"
    stt_model: str = Field(
        default="whisper-large-v3-turbo",
        validation_alias="LTS_MODEL",
    )
    stt_model_path: Path | None = None  # required for mlx-whisper readiness
    ollama_base_url: str = "http://127.0.0.1:11434"

    @property
    def db_path(self) -> Path:
        """Path to the SQLite jobs database."""
        return self.data_dir / "jobs.db"

    @property
    def audio_cache_dir(self) -> Path:
        """Stage 1 raw media downloads."""
        return self.data_dir / "audio-cache"

    @property
    def results_dir(self) -> Path:
        """Finished transcripts (Stage 3 output)."""
        return self.data_dir / "results"

    @property
    def trash_dir(self) -> Path:
        """Transcripts after extension download ack (HLD-001 §17 O-4)."""
        return self.data_dir / "trash"

    def ensure_dirs(self) -> None:
        """Create data_dir and all subdirectories if missing. Idempotent."""
        for d in (self.data_dir, self.audio_cache_dir, self.results_dir, self.trash_dir):
            d.mkdir(parents=True, exist_ok=True)

    @field_validator("data_dir", mode="before")
    @classmethod
    def _coerce_data_dir(cls, v: object) -> Path:
        """Accept strings and resolve to absolute Path with ~ expansion."""
        if isinstance(v, str):
            return Path(v).expanduser().resolve()
        if isinstance(v, Path):
            return v.expanduser().resolve()
        msg = f"unsupported type for data_dir: {type(v).__name__}"
        raise ValueError(msg)


def get_settings() -> Settings:
    """Load settings from the current process environment.

    Called once at service startup. Tests construct `Settings`
    directly with explicit values.
    """
    return Settings()  # type: ignore[call-arg]