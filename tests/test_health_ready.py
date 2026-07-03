"""Tests for `/health` and `/ready` endpoints (HLD-001 §8)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from httpx import AsyncClient

from local_transcription_service.api.health import (
    ReadinessReport,
    build_readiness_report,
)


async def test_health_returns_ok_and_version(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


def _override(report: ReadinessReport):
    """Wrap a static report in a request-shaped coroutine for `dependency_overrides`."""

    async def _fake(request: Request) -> ReadinessReport:  # noqa: ARG001
        return report

    return _fake


async def test_ready_returns_200_with_checks_when_all_pass(
    app: FastAPI, client: AsyncClient
) -> None:
    app.dependency_overrides[build_readiness_report] = _override(
        ReadinessReport(
            db_writable=True,
            ffmpeg_present=True,
            stt_engine="openai",
            stt_model_loaded=True,
        )
    )
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "checks": {
            "db_writable": True,
            "ffmpeg_present": True,
            "stt_engine": "openai",
            "stt_model_loaded": True,
        },
    }


async def test_ready_returns_503_when_db_unwritable(
    app: FastAPI, client: AsyncClient
) -> None:
    app.dependency_overrides[build_readiness_report] = _override(
        ReadinessReport(
            db_writable=False,
            ffmpeg_present=True,
            stt_engine="openai",
            stt_model_loaded=True,
        )
    )
    response = await client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["checks"]["db_writable"] is False


async def test_ready_returns_503_when_ffmpeg_missing(
    app: FastAPI, client: AsyncClient
) -> None:
    app.dependency_overrides[build_readiness_report] = _override(
        ReadinessReport(
            db_writable=True,
            ffmpeg_present=False,
            stt_engine="openai",
            stt_model_loaded=True,
        )
    )
    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["ffmpeg_present"] is False


async def test_ready_returns_503_when_stt_model_not_loaded(
    app: FastAPI, client: AsyncClient
) -> None:
    app.dependency_overrides[build_readiness_report] = _override(
        ReadinessReport(
            db_writable=True,
            ffmpeg_present=True,
            stt_engine="openai",
            stt_model_loaded=False,
        )
    )
    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["stt_model_loaded"] is False


async def test_ready_includes_stt_engine_field(
    app: FastAPI, client: AsyncClient
) -> None:
    """The configured engine is reported back to the client."""
    app.dependency_overrides[build_readiness_report] = _override(
        ReadinessReport(
            db_writable=True,
            ffmpeg_present=True,
            stt_engine="mock",
            stt_model_loaded=True,
        )
    )
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["stt_engine"] == "mock"


async def test_ready_does_not_require_auth(client: AsyncClient) -> None:
    # Default dependency probes real ffmpeg / STT gateway; outcome is
    # environment-dependent but must never be 401.
    response = await client.get("/ready")
    assert response.status_code in (200, 503)


# ---------- refactored dispatch: STT check goes through engine.is_ready() ----------
#
# TASK-B §B4: the inline dispatch on ``settings.stt_engine`` was
# removed from ``api/health.py``; the engine instance held by the
# pipeline is now the single source of truth for the
# ``stt_model_loaded`` flag. These tests exercise the actual
# ``_check_stt_engine`` function (not the dependency override) so
# that wiring is pinned.


class _RecordingEngine:
    """In-process STTEngine fake — records is_ready() calls."""

    def __init__(self, *, ready: bool) -> None:
        self._ready = ready
        self.calls: int = 0

    async def transcribe(self, wav_path, *, language=None):  # noqa: ANN001, ARG002
        raise NotImplementedError  # never invoked from a health check

    async def is_ready(self) -> bool:
        self.calls += 1
        return self._ready


class _PipelineWithEngine:
    """Minimal duck-typed pipeline exposing an ``engine`` property."""

    def __init__(self, engine: _RecordingEngine) -> None:
        self.engine = engine


async def test_check_stt_engine_delegates_to_pipeline_engine(tmp_path) -> None:
    """`_check_stt_engine` reads `pipeline.engine.is_ready()` — not
    `settings.stt_engine` directly. A pipeline holding a ready engine
    reports ``stt_model_loaded=True`` regardless of the
    ``stt_engine`` config string.
    """
    from local_transcription_service.api.health import _check_stt_engine
    from local_transcription_service.config import Settings

    engine = _RecordingEngine(ready=True)
    pipeline = _PipelineWithEngine(engine)
    settings = Settings(
        auth_token="x" * 32,
        data_dir=tmp_path,
        stt_engine="openai",  # any value; should be ignored by the dispatch
        stt_api_key="k",
    )

    label, ok = await _check_stt_engine(pipeline, settings)

    assert label == "openai"
    assert ok is True
    assert engine.calls == 1


async def test_check_stt_engine_returns_false_when_engine_not_ready(tmp_path) -> None:
    """A pipeline whose engine reports ``is_ready() == False`` makes
    the readiness check fail — the dispatch does NOT inspect
    ``settings.stt_engine`` to short-circuit a result.
    """
    from local_transcription_service.api.health import _check_stt_engine
    from local_transcription_service.config import Settings

    engine = _RecordingEngine(ready=False)
    pipeline = _PipelineWithEngine(engine)
    settings = Settings(
        auth_token="x" * 32,
        data_dir=tmp_path,
        stt_engine="mock",  # legacy short-circuit value; should be ignored
    )

    label, ok = await _check_stt_engine(pipeline, settings)

    assert label == "mock"
    assert ok is False
    assert engine.calls == 1


async def test_check_stt_engine_handles_missing_pipeline(tmp_path) -> None:
    """No pipeline wired → report ``stt_model_loaded=False`` but keep
    the ``stt_engine`` label so operators still see what was
    configured.
    """
    from local_transcription_service.api.health import _check_stt_engine
    from local_transcription_service.config import Settings

    settings = Settings(
        auth_token="x" * 32,
        data_dir=tmp_path,
        stt_engine="mock",
    )

    label, ok = await _check_stt_engine(None, settings)

    assert label == "mock"
    assert ok is False
