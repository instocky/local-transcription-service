"""Tests for the Settings class and HLD-mandated env-var names.

The env-var contract is the operator-facing surface; mismatches
between the HLD and the code produce silent failures at deploy
time (an operator who follows the HLD sets `LTS_PORT` and gets
the default port back). These tests pin the contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from local_transcription_service.config import Settings

_VALID_TOKEN = "x" * 32


def test_hld_env_var_names_are_picked_up(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """HLD-001 §4/§14: `LTS_MODEL` and `LTS_PORT` are the operator-facing names."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_PORT", "9999")
    monkeypatch.setenv("LTS_MODEL", "whisper-tiny")
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))

    s = Settings()

    assert s.bind_port == 9999
    assert s.stt_model == "whisper-tiny"


def test_drifted_env_names_are_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The pre-fix env names `LTS_BIND_PORT` / `LTS_STT_MODEL` must NOT
    be silently picked up — the contract is the HLD names only."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    # Set the OLD (drifted) names. They must not affect the resolved values.
    monkeypatch.setenv("LTS_BIND_PORT", "9999")
    monkeypatch.setenv("LTS_STT_MODEL", "whisper-tiny")
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))

    s = Settings()

    # Defaults still apply — the drifted names are unknown to the schema.
    assert s.bind_port == 8766
    assert s.stt_model == "whisper-large-v3-turbo"


def test_hld_env_var_names_are_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`case_sensitive=False` in SettingsConfigDict — operators can use
    either casing without breaking the contract."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("lts_port", "9999")
    monkeypatch.setenv("lts_model", "whisper-tiny")
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))

    s = Settings()

    assert s.bind_port == 9999
    assert s.stt_model == "whisper-tiny"


def test_auth_token_required() -> None:
    """`LTS_AUTH_TOKEN` is required — no default."""
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "auth_token" in str(exc_info.value)


def test_auth_token_minimum_length() -> None:
    """`LTS_AUTH_TOKEN` must be at least 16 characters."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(auth_token="too-short")
    assert "auth_token" in str(exc_info.value)


def test_defaults_match_hld(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Defaults match HLD-001 §14 (192.168.0.99:8766) and §4 (whisper-large-v3-turbo)."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    s = Settings()
    assert s.bind_host == "192.168.0.99"
    assert s.bind_port == 8766
    assert s.stt_model == "whisper-large-v3-turbo"
    assert s.ollama_base_url == "http://127.0.0.1:11434"
    assert s.max_attempts == 2
    assert s.retry_backoff_seconds == 30


def test_stt_engine_accepts_three_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`LTS_STT_ENGINE` accepts ollama / mlx-whisper / mock (HLD §4)."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    for engine in ("ollama", "mlx-whisper", "mock"):
        monkeypatch.setenv("LTS_STT_ENGINE", engine)
        assert Settings().stt_engine == engine


def test_stt_engine_rejects_unknown_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LTS_STT_ENGINE", "vosk")  # not in HLD-allowed set
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "stt_engine" in str(exc_info.value)
