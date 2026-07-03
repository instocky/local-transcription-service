"""Job submission and status endpoints.

All routes in this router require a valid `X-Auth-Token` header —
the dependency is applied at router level so individual route
decorators stay clean. Status codes and response shapes follow
HLD-001 §9.2.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from local_transcription_service.api.schemas import (
    JobStateResponse,
    SubmitJobRequest,
    SubmitJobResponse,
)
from local_transcription_service.auth import require_token
from local_transcription_service.models import JobStatus
from local_transcription_service.queue.store import JobStore

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_token)],
)


@router.post(
    "",
    response_model=SubmitJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Job accepted, see response body and Location header"},
        401: {"description": "Invalid or missing X-Auth-Token"},
        422: {"description": "Invalid or non-YouTube video_url"},
    },
)
async def submit_job(
    body: SubmitJobRequest,
    request: Request,
) -> SubmitJobResponse:
    """Submit a video URL for transcription.

    HLD-001 §9.2: returns 202 (not 201) and includes `poll_url` in
    the response body so clients can start polling without
    constructing the path themselves.
    """
    store: JobStore = request.app.state.store
    job = await store.submit(str(body.video_url))
    logger.info(
        "job submitted",
        extra={"job_id": job.job_id, "video_url": job.video_url},
    )
    return SubmitJobResponse(
        job_id=job.job_id,
        status=job.status,
        poll_url=f"/jobs/{job.job_id}",
    )


@router.get(
    "/{job_id}",
    response_model=JobStateResponse,
    responses={
        401: {"description": "Invalid or missing X-Auth-Token"},
        404: {"description": "Job not found"},
    },
)
async def get_job(job_id: str, request: Request) -> JobStateResponse:
    """Return current state of a job.

    For DONE jobs, the response also includes `transcript` (the
    full text) and `transcript_path` (HLD-001 §9.2). The file is
    read off the event loop via `asyncio.to_thread` so a slow
    disk doesn't block other requests.
    """
    store: JobStore = request.app.state.store
    job = await store.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "Job not found"},
        )

    transcript: str | None = None
    if job.status == JobStatus.DONE and job.transcript_path is not None:
        path = Path(job.transcript_path)
        if path.exists():  # noqa: ASYNC240 - fast local FS check; read happens in to_thread
            transcript = await asyncio.to_thread(path.read_text, "utf-8")

    return JobStateResponse(
        job_id=job.job_id,
        video_url=job.video_url,
        status=job.status,
        attempt=job.attempt,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
        transcript=transcript,
        transcript_path=job.transcript_path if job.status == JobStatus.DONE else None,
    )


@router.get(
    "/{job_id}/result",
    responses={
        200: {
            "content": {"text/plain": {}},
            "description": "Transcript text",
        },
        401: {"description": "Invalid or missing X-Auth-Token"},
        404: {"description": "Job not found or not yet done"},
        410: {"description": "Job failed"},
    },
)
async def get_result(job_id: str, request: Request) -> FileResponse:
    """Return the finished transcript file for a DONE job.

    Status codes:
    - 200: text/plain transcript file.
    - 404: job not found, or not yet done (poll `/jobs/{id}` instead).
    - 410: job is in FAILED state (gone — won't ever produce a result).
    - 500: DB says DONE but the file is missing on disk.
    """
    store: JobStore = request.app.state.store
    job = await store.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "Job not found"},
        )
    if job.status == JobStatus.FAILED:
        raise HTTPException(
            status_code=410,
            detail={"code": "JOB_FAILED", "message": "Job failed"},
        )
    if job.status != JobStatus.DONE:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "NOT_READY",
                "message": f"Job not done (status={job.status.value})",
            },
        )
    if job.transcript_path is None:
        raise HTTPException(  # pragma: no cover - defensive
            status_code=500,
            detail={"code": "TRANSCRIPT_MISSING", "message": "Transcript path not set"},
        )
    path = Path(job.transcript_path)
    if not path.exists():  # noqa: ASYNC240 - fast local FS check before streaming response
        raise HTTPException(
            status_code=500,
            detail={
                "code": "TRANSCRIPT_MISSING",
                "message": "Transcript file missing on disk",
            },
        )
    return FileResponse(path, media_type="text/plain; charset=utf-8")
