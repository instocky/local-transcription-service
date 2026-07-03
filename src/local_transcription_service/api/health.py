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
        "stt_engine": "ollama",
        "stt_model_loaded": true
      }
    }

The STT check dispatches on `settings.stt_engine` so mlx-whisper
deployments don't get probed against ollama (and vice versa).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from local_transcription_service import __version__
from local_transcription_service.config import Settings
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
    stt_engine: str  # "ollama" | "mlx-whisper" | "mock"
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


async def _check_ollama_model(base_url: str, model: str) -> bool:
    """GET `{ollama}/api/tags` and confirm the configured model is loaded.

    `model` may include a tag suffix (e.g. `whisper-large-v3-turbo:q5`);
    we compare on the bare name so the check tolerates the user's
    quantization choice.
    """
    url = f"{base_url.rstrip('/')}/api/tags"
    target = model.split(":")[0]
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return False
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("readiness: ollama model check failed: %s", exc)
        return False
    models = [m.get("name", "").split(":")[0] for m in payload.get("models", [])]
    return target in models


async def _check_mlx_whisper_model(path: Path | None) -> bool:
    """mlx-whisper readiness: the model file exists and is readable.

    The path is configured via `LTS_STT_MODEL_PATH`; if not set we
    report not-ready (rather than guessing a default location).
    """
    if path is None:
        return False
    try:
        exists = await asyncio.to_thread(path.is_file)
    except OSError as exc:
        logger.warning("readiness: mlx-whisper model check failed: %s", exc)
        return False
    return exists


async def _check_stt_engine(settings: Settings) -> bool:
    """Dispatch the STT check on the configured engine (HLD-001 §8)."""
    if settings.stt_engine == "mock":
        # Mock pipeline is in-process; no external dependency to probe.
        return True
    if settings.stt_engine == "ollama":
        return await _check_ollama_model(settings.ollama_base_url, settings.stt_model)
    if settings.stt_engine == "mlx-whisper":
        return await _check_mlx_whisper_model(settings.stt_model_path)
    logger.warning("readiness: unknown stt_engine=%r", settings.stt_engine)
    return False


# ---------- route + dependency ----------


async def build_readiness_report(request: Request) -> ReadinessReport:
    """Run all readiness checks concurrently.

    Exposed as a FastAPI dependency so tests can override it
    without monkey-patching subprocesses or HTTP.
    """
    settings: Settings = request.app.state.settings
    store: JobStore = request.app.state.store
    db_ok, ff_ok, stt_ok = await asyncio.gather(
        _check_db_writable(store),
        _check_ffmpeg(),
        _check_stt_engine(settings),
    )
    return ReadinessReport(
        db_writable=db_ok,
        ffmpeg_present=ff_ok,
        stt_engine=settings.stt_engine,
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
