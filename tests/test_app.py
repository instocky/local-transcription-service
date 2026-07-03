"""Tests for ``app.create_app`` and the production wiring helpers.

``create_app(settings, store, pipeline)`` keeps its Phase A DI shape
(Task-B §B4: "create_app(settings, store, pipeline) keeps its
current DI shape"). These tests pin the contract:

- the three dependencies are stashed on ``app.state`` so endpoint
  handlers can reach them via ``request.app.state.*``;
- ``pipeline=None`` is accepted (some tests build a bare app);
- ``build_stt_engine`` picks ``MockSTT`` for ``stt_engine='mock'``
  and ``LiteLLMWhisperSTT`` for ``stt_engine='openai'``;
- ``build_pipeline`` returns a ``WhisperPipeline`` with the engine
  wired through;
- ``create_app`` does NOT pull dependencies from settings — the
  caller is always the owner of ``pipeline`` (so tests can pass
  ``MockPipeline`` without needing a real STT gateway).

The health-check side of the wiring (delegating to
``STTEngine.is_ready()``) is covered by ``test_health.py`` /
``test_health_ready.py``.
"""

from __future__ import annotations

import pytest

from local_transcription_service.app import (
    build_pipeline,
    build_stt_engine,
    create_app,
)
from local_transcription_service.config import Settings
from local_transcription_service.pipeline.base import MockPipeline
from local_transcription_service.pipeline.whisper_pipeline import WhisperPipeline
from local_transcription_service.queue.store import JobStore
from local_transcription_service.stt.litellm_whisper import LiteLLMWhisperSTT
from local_transcription_service.stt.mock import MockSTT

AUTH_TOKEN = "test-token-aaaaaaaaaa"


# ---------- create_app: DI contract ----------


async def test_create_app_stashes_settings_store_and_pipeline_on_state(
    tmp_path,
) -> None:
    settings = Settings(
        auth_token=AUTH_TOKEN,
        data_dir=tmp_path,
        stt_engine="mock",  # avoid openai-requires-api-key validator
    )
    store = JobStore(settings.db_path)
    await store.init()
    pipeline = MockPipeline()

    app = create_app(settings=settings, store=store, pipeline=pipeline)

    assert app.state.settings is settings
    assert app.state.store is store
    assert app.state.pipeline is pipeline


async def test_create_app_accepts_none_pipeline(tmp_path) -> None:
    """`pipeline=None` is accepted — some tests construct a bare
    app and never hit the worker / pipeline path."""
    settings = Settings(
        auth_token=AUTH_TOKEN,
        data_dir=tmp_path,
        stt_engine="mock",
    )
    store = JobStore(settings.db_path)
    await store.init()

    app = create_app(settings=settings, store=store, pipeline=None)
    assert app.state.pipeline is None


async def test_create_app_registers_routers(tmp_path) -> None:
    """Both the health and jobs routers are registered. Pinning this
    so a future refactor that forgets one fails loudly.

    FastAPI ≥ 0.139 wraps each ``include_router`` call in an
    ``_IncludedRouter`` entry on ``app.routes``; the actual route
    objects live under ``_IncludedRouter.original_router.routes``.
    We walk both shapes so the test stays correct against the
    current FastAPI version.
    """
    from fastapi import FastAPI

    from local_transcription_service.api.health import router as health_router
    from local_transcription_service.api.jobs import router as jobs_router

    settings = Settings(
        auth_token=AUTH_TOKEN,
        data_dir=tmp_path,
        stt_engine="mock",
    )
    store = JobStore(settings.db_path)
    await store.init()
    app: FastAPI = create_app(settings=settings, store=store, pipeline=MockPipeline())

    paths: set[str] = set()
    for r in app.routes:
        if hasattr(r, "path"):
            paths.add(r.path)
        elif type(r).__name__ == "_IncludedRouter":
            original = getattr(r, "original_router", None)
            if original is not None:
                paths.update(
                    sub.path for sub in original.routes if hasattr(sub, "path")
                )

    assert "/health" in paths
    assert "/ready" in paths
    assert "/jobs" in paths

    # And the routers that ship those paths are the expected ones.
    assert health_router.routes and any(
        getattr(r, "path", None) in paths for r in health_router.routes
    )
    assert jobs_router.routes and any(
        getattr(r, "path", None) in paths for r in jobs_router.routes
    )


# ---------- build_stt_engine: dispatch on settings.stt_engine ----------


def test_build_stt_engine_mock_returns_mock_stt(tmp_path) -> None:
    """`stt_engine='mock'` → MockSTT (no I/O, the CI / dev path)."""
    settings = Settings(
        auth_token=AUTH_TOKEN,
        data_dir=tmp_path,
        stt_engine="mock",
    )
    engine = build_stt_engine(settings)
    assert isinstance(engine, MockSTT)


def test_build_stt_engine_openai_returns_litellm_whisper_stt(tmp_path) -> None:
    """`stt_engine='openai'` → LiteLLMWhisperSTT wired with the
    operator-facing settings (base_url, api_key, model)."""
    settings = Settings(
        auth_token=AUTH_TOKEN,
        data_dir=tmp_path,
        stt_engine="openai",
        stt_api_key="test-key",
        stt_base_url="http://gateway.test:4000/v1",
        stt_model="whisper-tiny",
    )
    engine = build_stt_engine(settings)
    assert isinstance(engine, LiteLLMWhisperSTT)
    assert engine._base_url == "http://gateway.test:4000/v1"  # noqa: SLF001 (test internals)
    assert engine._api_key == "test-key"  # noqa: SLF001
    assert engine._model == "whisper-tiny"  # noqa: SLF001


def test_build_stt_engine_rejects_unknown_value(tmp_path) -> None:
    """The Literal type guards `stt_engine` at `Settings(...)` parse
    time, but `build_stt_engine` also raises ValueError if it sees
    something unexpected — defensive for callers that bypass the
    validator (custom Settings subclasses in tests, etc.)."""
    settings = Settings(
        auth_token=AUTH_TOKEN,
        data_dir=tmp_path,
        stt_engine="mock",
    )
    # Sneak past the Literal by mutating the field directly.
    object.__setattr__(settings, "stt_engine", "unknown-thing")
    with pytest.raises(ValueError, match="unsupported stt_engine"):
        build_stt_engine(settings)


# ---------- build_pipeline: WhisperPipeline wiring ----------


def test_build_pipeline_returns_whisper_pipeline_with_engine(tmp_path) -> None:
    """The production pipeline is a ``WhisperPipeline`` and holds the
    engine passed in (so ``/ready`` can call ``engine.is_ready()``).
    """
    settings = Settings(
        auth_token=AUTH_TOKEN,
        data_dir=tmp_path,
        stt_engine="mock",
    )
    engine = MockSTT()
    pipeline = build_pipeline(settings, engine)

    assert isinstance(pipeline, WhisperPipeline)
    assert pipeline.engine is engine


def test_build_pipeline_uses_settings_audio_cache_dir(tmp_path) -> None:
    """Stage 1 + Stage 2 read/write under ``settings.audio_cache_dir``;
    the pipeline must be constructed with that exact path so the
    worker's temp-file layout matches what the settings promises."""
    settings = Settings(
        auth_token=AUTH_TOKEN,
        data_dir=tmp_path,
        stt_engine="mock",
    )
    engine = MockSTT()
    pipeline = build_pipeline(settings, engine)

    assert pipeline._audio_cache_dir == settings.audio_cache_dir  # noqa: SLF001