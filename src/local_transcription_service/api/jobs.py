"""Job submission and status endpoints.

All routes in this router require a valid `X-Auth-Token` header —
the dependency is applied at router level so individual route
decorators stay clean. Status codes and response shapes follow
HLD-001 §6.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from local_transcription_service.api.schemas import (
    AckResponse,
    JobStateResponse,
    SubmitJobRequest,
    SubmitJobResponse,
)
from local_transcription_service.auth import require_token
from local_transcription_service.config import Settings
from local_transcription_service.models import JobStatus
from local_transcription_service.queue.store import JobStore
from local_transcription_service.queue.transcripts import MoveOutcome, move_to_trash

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

    HLD-001 §6: returns 202 (not 201) and includes `poll_url` in
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
    full text) and `transcript_path` (HLD-001 §6). The file is
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
        acked_at=job.acked_at,
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


@router.post(
    "/{job_id}/ack",
    response_model=AckResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Ack accepted (first call or idempotent retry)"},
        401: {"description": "Invalid or missing X-Auth-Token"},
        404: {"description": "Job not found"},
        409: {"description": "Job is not in DONE state — cannot ack"},
    },
)
async def ack_job(job_id: str, request: Request) -> AckResponse:
    """Mark a finished job as downloaded by the extension (HLD-001 §13.1).

    Behaviour:

    - First call after ``mark_done``: sets ``acked_at``, moves the
      transcript file from its current path into ``trash/`` (the
      destination filename is preserved from the source — see
      HLD §13.1 for the rationale). Returns ``already_acked=False``.
    - Repeat call on an already-acked job: ``acked_at`` is preserved
      (not bumped to "now"). The DB row is the source of truth; the
      FS move is re-attempted iff the file is not actually in ``trash/``
      AND exists on disk — so a retry after the operator clears a
      transient FS problem (full disk, permission, manual cleanup)
      eventually converges. Returns ``already_acked=True``.
    - For a non-DONE job (``queued``/``claimed``/``processing``/
      ``failed``): ``409`` with ``code="NOT_DONE"``. Acks are
      meaningless before the transcript file exists.

    ``transcript_moved`` reflects the **observed filesystem state at
    return time** — ``True`` iff the file path the DB points at
    currently exists on disk AND its parent is the trash directory.
    A pure path check is not enough: an operator may delete the file
    from trash manually, and a stale DB path would otherwise
    mislead the extension. With ``Path.exists()`` included, the
    fallback `move_to_trash` re-runs against the source path
    (no-op if source == destination), and the response surfaces
    ``transcript_moved=False`` accurately.

    Failure handling is documented in HLD-001 §13.1 ("Failure-mode
    contract"). The short version:

    - DB write fails → ``503 Service Unavailable`` (mapped from
      ``aiosqlite.Error`` / ``sqlite3.Error``). Two sub-cases:
        a. ``mark_acked`` raises before any FS work happened —
           ``200`` retry is trivially safe.
        b. ``update_transcript_path`` raises after a successful
           FS move (the partial-state window) — the file is in
           ``trash/`` but the DB path is stale. The retry
           **auto-discovers** the canonical trash file via
           ``move_to_trash`` and heals the DB path; ``GET
           /jobs/{id}/result`` recovers. Pinned by
           ``test_ack_converges_after_update_transcript_path_failure``.
    - FS move fails (any reason — permission, cross-volume, full disk,
      source missing under no-canonical) → ``200`` with
      ``transcript_moved=False``; ``acked_at`` is set if the DB
      write succeeded. Operator can drag the file to ``trash/``
      manually; a subsequent ack will report ``transcript_moved=True``
      when the file lands there.
    """
    store: JobStore = request.app.state.store
    settings: Settings = request.app.state.settings

    # Single try-block wrapping all DB-touching calls so a transient
    # database failure surfaces uniformly as 503. Our own HTTPException
    # (404 / 409) is re-raised unchanged below. Unknown exceptions fall
    # through to FastAPI's default 500 handler.
    try:
        # Pre-flight check: 404 vs 409 mapping is the endpoint's job,
        # not the store's. We refresh after `mark_acked` so the response
        # carries the canonical `acked_at` whether the row was just
        # written or already had a value from a previous call.
        pre = await store.get(job_id)
        if pre is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Job not found"},
            )
        if pre.status != JobStatus.DONE:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "NOT_DONE",
                    "message": (
                        f"Job not in DONE state (status={pre.status.value}); "
                        "only finished jobs can be acked"
                    ),
                },
            )

        job, newly_acked = await store.mark_acked(job_id)

        if newly_acked:
            logger.info(
                "ack_job: first ack — moving transcript to trash",
                extra={
                    "job_id": job_id,
                    "event": "job_acked",
                    "acked_at": job.acked_at.isoformat() if job.acked_at else None,
                },
            )
        else:
            logger.info(
                "ack_job: idempotent retry — re-attempting FS move if needed",
                extra={
                    "job_id": job_id,
                    "event": "job_ack_idempotent",
                    "acked_at": job.acked_at.isoformat() if job.acked_at else None,
                },
            )

        # FS move: skip iff the persisted transcript_path currently
        # points inside trash_dir AND the file exists on disk. We
        # re-attempt the move when either condition is false — this
        # is what makes a retry-after-FS-issue (or after-operator-
        # manual-cleanup) converge instead of leaving the file
        # stranded or reporting a stale "moved" state.
        transcript_path_obj = (
            Path(str(job.transcript_path)) if job.transcript_path else None
        )
        currently_in_trash = (
            transcript_path_obj is not None
            and transcript_path_obj.parent.resolve() == settings.trash_dir.resolve()
            and transcript_path_obj.exists()
        )
        if currently_in_trash:
            outcome = MoveOutcome(
                moved=False,
                destination=transcript_path_obj,
            )
        else:
            outcome = move_to_trash(
                job_id=job_id,
                source=transcript_path_obj,
                trash_dir=settings.trash_dir,
            )
            if outcome.destination is not None:
                await store.update_transcript_path(job_id, str(outcome.destination))
                # Refresh so the response carries the updated path.
                job = await store.get(job_id) or job

        # `transcript_moved` reflects the **observed** state at return
        # time — same predicate as `currently_in_trash` evaluated
        # after the move + path-update. An operator who deleted the
        # file from trash manually will see `transcript_moved=False`
        # here, and the extension can react accordingly.
        final_path_obj = (
            Path(str(job.transcript_path)) if job.transcript_path else None
        )
        transcript_moved_now = (
            final_path_obj is not None
            and final_path_obj.parent.resolve() == settings.trash_dir.resolve()
            and final_path_obj.exists()
        )
    except HTTPException:
        raise  # pass through our own 4xx mapping unchanged
    except (aiosqlite.Error, sqlite3.Error) as exc:
        logger.error(
            "ack_job: database failure",
            exc_info=True,
            extra={"job_id": job_id, "event": "db_failure"},
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DB_UNAVAILABLE",
                "message": f"Database error: {exc}",
            },
        ) from exc

    return AckResponse(
        job_id=job.job_id,
        acked_at=job.acked_at,  # type: ignore[arg-type]  # guaranteed non-None after mark_acked
        already_acked=not newly_acked,
        transcript_moved=transcript_moved_now,
        transcript_path=(
            str(outcome.destination) if outcome.destination is not None else job.transcript_path
        ),
    )
