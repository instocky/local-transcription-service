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
            stt_engine="ollama",
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
            "stt_engine": "ollama",
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
            stt_engine="ollama",
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
            stt_engine="ollama",
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
            stt_engine="ollama",
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
            stt_engine="mlx-whisper",
            stt_model_loaded=True,
        )
    )
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["stt_engine"] == "mlx-whisper"


async def test_ready_does_not_require_auth(client: AsyncClient) -> None:
    # Default dependency probes real ffmpeg/ollama; outcome is
    # environment-dependent but must never be 401.
    response = await client.get("/ready")
    assert response.status_code in (200, 503)
