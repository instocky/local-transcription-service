"""Tests for the Settings class and HLD-mandated env-var names.

The env-var contract is the operator-facing surface; mismatches
between the HLD and the code produce silent failures at deploy
time (an operator who follows the HLD sets `LTS_PORT` and gets
the default port back). These tests pin the contract.

Phase B (B5a): the engine is locked to whisper.cpp behind LiteLLM
(`openai` protocol), not ollama. So `LTS_STT_BASE_URL` + `LTS_STT_API_KEY`
are the operator-facing entries; `LTS_OLLAMA_BASE_URL` / `stt_model_path`
are gone. See HLD-001 §4 (amended 2026-07-03).
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
    monkeypatch.setenv("LTS_STT_ENGINE", "mock")  # bypass openai-api-key rule

    s = Settings()

    assert s.bind_port == 9999
    assert s.stt_model == "whisper-tiny"


def test_drifted_env_names_are_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The pre-fix env names `LTS_BIND_PORT` / `LTS_STT_MODEL` /
    `LTS_OLLAMA_BASE_URL` / `LTS_STT_MODEL_PATH` must NOT be silently
    picked up — the contract is the HLD names only."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_STT_ENGINE", "mock")  # bypass openai-api-key rule
    # Set the OLD (drifted / removed) names. They must not affect the resolved values.
    monkeypatch.setenv("LTS_BIND_PORT", "9999")
    monkeypatch.setenv("LTS_STT_MODEL", "whisper-tiny")
    monkeypatch.setenv("LTS_OLLAMA_BASE_URL", "http://drifted:11434")
    monkeypatch.setenv("LTS_STT_MODEL_PATH", "/tmp/does-not-exist")
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))

    s = Settings()

    # Defaults still apply — the drifted names are unknown to the schema.
    assert s.bind_port == 8766
    assert s.stt_model == "whisper-large-v3-turbo"
    assert s.stt_base_url == "http://192.168.0.99:4000/v1"


def test_hld_env_var_names_are_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`case_sensitive=False` in SettingsConfigDict — operators can use
    either casing without breaking the contract."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("lts_port", "9999")
    monkeypatch.setenv("lts_model", "whisper-tiny")
    monkeypatch.setenv("lts_stt_base_url", "http://lowercase:4000/v1")
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LTS_STT_ENGINE", "mock")  # bypass openai-api-key rule

    s = Settings()

    assert s.bind_port == 9999
    assert s.stt_model == "whisper-tiny"
    assert s.stt_base_url == "http://lowercase:4000/v1"


def test_auth_token_required(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """`LTS_AUTH_TOKEN` is required — no default."""
    # Pin everything else so we isolate the missing-token error.
    monkeypatch.setenv("LTS_STT_ENGINE", "mock")
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "auth_token" in str(exc_info.value)


def test_auth_token_minimum_length(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """`LTS_AUTH_TOKEN` must be at least 16 characters."""
    monkeypatch.setenv("LTS_STT_ENGINE", "mock")
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LTS_AUTH_TOKEN", "too-short")
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "auth_token" in str(exc_info.value)


def test_defaults_match_hld(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Defaults match HLD-001 §14 (192.168.0.99:8766) and §4 (LiteLLM
    gateway + `whisper-large-v3-turbo`, `openai` engine).

    Operator-facing defaults — `LTS_STT_ENGINE` is **not** set in the
    env so the field default (`openai`) applies. We satisfy the
    validator by passing the api key through `Settings(...)` directly
    rather than the env."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LTS_STT_ENGINE", raising=False)

    s = Settings(stt_api_key="master-key")

    assert s.bind_host == "192.168.0.99"
    assert s.bind_port == 8766
    assert s.stt_engine == "openai"  # field default
    assert s.stt_base_url == "http://192.168.0.99:4000/v1"
    assert s.stt_model == "whisper-large-v3-turbo"
    assert s.max_attempts == 2
    assert s.retry_backoff_seconds == 30


def test_stt_api_key_field_defaults_to_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Field default for `stt_api_key` is `""` — the mock engine path
    is then valid without any other config."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_STT_ENGINE", "mock")
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LTS_STT_API_KEY", raising=False)

    s = Settings()
    assert s.stt_api_key == ""


def test_stt_engine_accepts_two_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`LTS_STT_ENGINE` accepts `openai` / `mock` (HLD §4 amended)."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))

    # engine=mock does not require stt_api_key.
    monkeypatch.setenv("LTS_STT_ENGINE", "mock")
    assert Settings().stt_engine == "mock"

    # engine=openai requires stt_api_key (covered in its own test below)
    # but the value itself is accepted.
    monkeypatch.setenv("LTS_STT_ENGINE", "openai")
    monkeypatch.setenv("LTS_STT_API_KEY", "some-key")
    assert Settings().stt_engine == "openai"


def test_stt_engine_rejects_removed_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`ollama` and `mlx-whisper` are gone from the Literal in HLD-001 §4 (amended)."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    for removed in ("ollama", "mlx-whisper", "vosk"):
        monkeypatch.setenv("LTS_STT_ENGINE", removed)
        with pytest.raises(ValidationError) as exc_info:
            Settings()
        assert "stt_engine" in str(exc_info.value)


def test_openai_engine_requires_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """`stt_engine == 'openai'` + empty `stt_api_key` → model_validator error.

    Without this rule the service would start and only fail on the
    first STT call with an opaque 401.
    """
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LTS_STT_ENGINE", "openai")
    monkeypatch.delenv("LTS_STT_API_KEY", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings()
    msg = str(exc_info.value)
    assert "stt_api_key" in msg
    assert "LTS_STT_API_KEY" in msg


def test_openai_engine_default_without_api_key_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The default `stt_engine` is `openai`. An operator with no
    `LTS_STT_API_KEY` in env must see the validator error immediately
    rather than after the first transcript."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    # No LTS_STT_ENGINE at all → default = "openai".
    monkeypatch.delenv("LTS_STT_ENGINE", raising=False)
    monkeypatch.delenv("LTS_STT_API_KEY", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "stt_api_key" in str(exc_info.value)


def test_mock_engine_does_not_require_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`stt_engine == 'mock'` + empty `stt_api_key` is the expected CI
    config (no gateway reachable, no token needed)."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LTS_STT_ENGINE", "mock")
    monkeypatch.setenv("LTS_STT_API_KEY", "")

    s = Settings()  # should not raise
    assert s.stt_engine == "mock"
    assert s.stt_api_key == ""


def test_stt_base_url_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """`LTS_STT_BASE_URL` overrides the default gateway (LiteLLM :4000)."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LTS_STT_ENGINE", "mock")
    monkeypatch.setenv("LTS_STT_BASE_URL", "http://gateway.example:4000/v1")

    s = Settings()
    assert s.stt_base_url == "http://gateway.example:4000/v1"


def test_stt_api_key_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """`LTS_STT_API_KEY` is the bearer token for the STT gateway."""
    monkeypatch.setenv("LTS_AUTH_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LTS_STT_ENGINE", "openai")
    monkeypatch.setenv("LTS_STT_API_KEY", "sk-litellm-master-key")

    s = Settings()
    assert s.stt_api_key == "sk-litellm-master-key"