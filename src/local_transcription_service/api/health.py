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

The STT check dispatches on `settings.stt_engine`. Under the
LiteLLM contract (`stt_engine == "openai"`) the probe hits
`{stt_base_url}/models` with the configured bearer token and
confirms `stt_model` is registered; under `mock` it short-circuits
to ready (no external dependency).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass

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


async def _check_openai_model(base_url: str, model: str, api_key: str) -> bool:
    """Confirm `model` is served by the OpenAI-compatible STT gateway.

    Pings ``{base_url}/models`` with the configured bearer token.
    The list is the standard OpenAI response (`{"data": [{"id": ...}]}`);
    LiteLLM Proxy forwards it as-is. We compare on the bare name so a
    tag-style identifier (e.g. ``whisper-large-v3-turbo:q5``) still
    matches the registered deployment.
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    target = model.split(":")[0]
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(url, headers=headers)
        if response.status_code != 200:
            return False
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("readiness: openai model check failed: %s", exc)
        return False
    models: list[str] = []
    for entry in payload.get("data", []):
        model_id = entry.get("id", "")
        if model_id:
            models.append(model_id.split(":")[0])
    return target in models


async def _check_stt_engine(settings: Settings) -> bool:
    """Dispatch the STT check on the configured engine (HLD-001 §8)."""
    if settings.stt_engine == "mock":
        # Mock pipeline is in-process; no external dependency to probe.
        return True
    if settings.stt_engine == "openai":
        return await _check_openai_model(
            settings.stt_base_url, settings.stt_model, settings.stt_api_key
        )
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