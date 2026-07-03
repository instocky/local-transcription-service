"""Health and readiness endpoints.

Both are unauthenticated (HLD-001 §14): even on a LAN, liveness
probes from infrastructure (load balancers, orchestrators) need
to reach the endpoint without sharing secrets.

`/ready` response shape follows HLD-001 §8:

    {
      "ready": true,
      "checks": {
        "db_writable": true,
        "ffmpeg_present": true,
        "stt_engine": "openai",
        "stt_model_loaded": true
      }
    }

The STT check delegates to ``STTEngine.is_ready()`` on the engine
instance held by the configured pipeline (TASK-B §B4). This makes
the engine the single source of truth: adding a new engine
implementation later (e.g. ``mlx-whisper``) does not require
editing this module.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from local_transcription_service import __version__
from local_transcription_service.config import Settings
from local_transcription_service.pipeline.base import TranscriptionPipeline
from local_transcription_service.queue.store import JobStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@dataclass(frozen=True)
class ReadinessReport:
    """Result of the readiness probes (HLD-001 §8).

    `ready` is True only when every individual check passes. The
    HTTP response status is derived from `ready` (200 vs 503).
    """

    db_writable: bool
    ffmpeg_present: bool
    stt_engine: str  # "openai" | "mock"
    stt_model_loaded: bool

    @property
    def ready(self) -> bool:
        return self.db_writable and self.ffmpeg_present and self.stt_model_loaded

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "checks": {
                "db_writable": self.db_writable,
                "ffmpeg_present": self.ffmpeg_present,
                "stt_engine": self.stt_engine,
                "stt_model_loaded": self.stt_model_loaded,
            },
        }


# ---------- individual checks ----------


async def _check_db_writable(store: JobStore) -> bool:
    """Verify the DB file is writable (HLD-001 §8).

    Delegates to `JobStore.ping_writable`, which issues a
    `BEGIN IMMEDIATE` to force a real write-lock attempt. A plain
    `SELECT` would only confirm the file is readable — which is
    not what the readiness contract promises.
    """
    return await store.ping_writable()


async def _check_ffmpeg() -> bool:
    """`ffmpeg -version` with a short timeout. Missing binary -> not ready."""
    if shutil.which("ffmpeg") is None:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except (TimeoutError, OSError) as exc:
        logger.warning("readiness: ffmpeg check failed: %s", exc)
        return False
    else:
        return proc.returncode == 0


async def _check_stt_engine(
    pipeline: TranscriptionPipeline | None,
    settings: Settings,
) -> tuple[str, bool]:
    """Probe the configured STT engine via its instance method (TASK-B §B4).

    Returns ``(stt_engine_label, stt_model_loaded)``:

    - ``stt_engine_label`` is reported back to the client so
      operators can confirm which backend the running service is
      wired against. It comes from ``settings.stt_engine`` — the
      config flag — not the engine's runtime class, so it stays
      aligned with the operator-facing contract.
    - ``stt_model_loaded`` is whatever the engine's own
      :meth:`is_ready` reports. The ``STTEngine`` protocol
      guarantees ``is_ready`` does not raise (a gateway outage
      returns ``False``, never an exception), so we don't have to
      wrap this call.

    The previous implementation dispatched inline on
    ``settings.stt_engine`` (``ollama`` / ``mlx-whisper`` / ``mock``).
    That has been replaced: there is one source of truth now — the
    engine itself. Adding a new engine implementation later does
    not require touching this module.
    """
    label = settings.stt_engine
    if pipeline is None:
        # No pipeline wired (tests sometimes construct a bare app).
        logger.warning("readiness: no pipeline configured; reporting STT not ready")
        return label, False
    engine = getattr(pipeline, "engine", None)
    if engine is None:
        # Defensive fallback for a pipeline that hasn't exposed the
        # ``engine`` property (e.g. a future custom subclass).
        logger.warning("readiness: pipeline has no engine; reporting STT not ready")
        return label, False
    return label, await engine.is_ready()


# ---------- route + dependency ----------


async def build_readiness_report(request: Request) -> ReadinessReport:
    """Run all readiness checks concurrently.

    Exposed as a FastAPI dependency so tests can override it
    without monkey-patching subprocesses or HTTP.
    """
    settings: Settings = request.app.state.settings
    store: JobStore = request.app.state.store
    pipeline: TranscriptionPipeline | None = getattr(request.app.state, "pipeline", None)
    db_ok, ff_ok, (stt_label, stt_ok) = await asyncio.gather(
        _check_db_writable(store),
        _check_ffmpeg(),
        _check_stt_engine(pipeline, settings),
    )
    return ReadinessReport(
        db_writable=db_ok,
        ffmpeg_present=ff_ok,
        stt_engine=stt_label,
        stt_model_loaded=stt_ok,
    )


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Returns 200 if the process can serve HTTP."""
    return {"status": "ok", "version": __version__}


@router.get(
    "/ready",
    response_model=None,
    responses={
        200: {"description": "All readiness checks passed"},
        503: {"description": "At least one readiness check failed"},
    },
)
async def ready(
    report: ReadinessReport = Depends(build_readiness_report),  # noqa: B008
) -> JSONResponse:
    """Readiness probe. 200 if ready, 503 otherwise (HLD-001 §8).

    Body is always the same `ReadinessReport.to_dict()` shape so
    clients can inspect individual checks regardless of status.
    """
    status_code = status.HTTP_200_OK if report.ready else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(content=report.to_dict(), status_code=status_code)