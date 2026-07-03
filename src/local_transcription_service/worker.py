"""Background worker that drains the job queue.

Two concurrent loops share one `JobStore`:

- **Claim loop**: pull the oldest QUEUED job, run the pipeline,
  mark DONE or FAILED.
- **Reclaim loop**: every `reclaim_interval_seconds`, return jobs
  whose lease has expired back to QUEUED so a healthy worker can
  pick them up.

SQLite's write-lock serializes all updates, so no extra
coordination is needed between loops. The two loops run in the
same event loop as the FastAPI app in production (`app.main`).

Retry policy (HLD-001 §10): up to `max_attempts` (default 2)
processing attempts per job. Retryable pipeline failures defer
the job with `next_retry_at = now + retry_backoff_seconds`
(30 s by default). Non-retryable failures — and retryable
failures that exhaust the attempt budget — go straight to
FAILED (terminal).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from local_transcription_service.config import Settings
from local_transcription_service.models import Job, JobError
from local_transcription_service.pipeline.base import PipelineError, TranscriptionPipeline
from local_transcription_service.queue.store import JobStore

logger = logging.getLogger(__name__)

# Tunables for the idle wait in the claim loop. Short enough to react
# quickly to a new submission, long enough to avoid burning CPU when
# the queue is empty.
_CLAIM_POLL_IDLE_SECONDS: float = 0.5


class Worker:
    """Async job-processing worker.

    Use `process_one()` to drive a single iteration (tests, manual
    draining) or `run_forever()` to start both loops (production).
    `stop()` signals the loops to exit on the next idle check.
    """

    def __init__(
        self,
        store: JobStore,
        pipeline: TranscriptionPipeline,
        settings: Settings,
    ) -> None:
        self._store = store
        self._pipeline = pipeline
        self._settings = settings
        self._stop = asyncio.Event()

    # ---------- lifecycle ----------

    def stop(self) -> None:
        """Signal the run loop to exit on the next iteration."""
        self._stop.set()

    async def run_forever(self) -> None:
        """Run claim and reclaim loops until `stop()` is called."""
        claim_task = asyncio.create_task(self._claim_loop(), name="lts-claim")
        reclaim_task = asyncio.create_task(self._reclaim_loop(), name="lts-reclaim")
        try:
            await self._stop.wait()
        finally:
            claim_task.cancel()
            reclaim_task.cancel()
            await asyncio.gather(claim_task, reclaim_task, return_exceptions=True)

    # ---------- single-iteration entry points (test-friendly) ----------

    async def process_one(self) -> bool:
        """Process at most one job. Returns True if a job was processed.

        Respects `max_attempts`: if the just-claimed job's `attempt`
        counter has already exceeded the limit, it is marked FAILED
        with code `MAX_ATTEMPTS` and not re-run.
        """
        job = await self._store.claim(
            lease_ttl_seconds=self._settings.lease_ttl_seconds,
        )
        if job is None:
            return False
        assert job.lease_token is not None, "claim must populate lease_token"

        if job.attempt > self._settings.max_attempts:
            await self._store.mark_failed(
                job.job_id,
                lease_token=job.lease_token,
                error=JobError(
                    code="MAX_ATTEMPTS",
                    message=f"Job exceeded {self._settings.max_attempts} attempts",
                    retryable=False,
                ),
            )
            logger.info("job failed: max attempts", extra={"job_id": job.job_id})
            return True

        moved = await self._store.mark_processing(
            job.job_id,
            lease_token=job.lease_token,
        )
        if not moved:
            logger.info(
                "claim lost before processing",
                extra={"job_id": job.job_id},
            )
            return True

        try:
            text = await self._pipeline.transcribe(job.video_url, job_id=job.job_id)
        except Exception as exc:  # noqa: BLE001 - any pipeline failure is a job failure
            error = self._error_from_exception(exc)
            await self._handle_pipeline_failure(job, error)
            return True

        transcript_path = self._settings.results_dir / f"{job.job_id}.txt"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(text, encoding="utf-8")

        done = await self._store.mark_done(
            job.job_id,
            lease_token=job.lease_token,
            transcript_path=str(transcript_path),
        )
        if not done:
            logger.warning(
                "mark_done rejected (lease lost)",
                extra={"job_id": job.job_id},
            )
        else:
            logger.info("job done", extra={"job_id": job.job_id})
        return True

    async def reclaim_once(self) -> int:
        """One reclaim pass. Returns the number of jobs returned to QUEUED."""
        n = await self._store.reclaim_expired()
        if n:
            logger.info("reclaimed %d expired job(s)", n)
        return n

    # ---------- long-running loops ----------

    async def _claim_loop(self) -> None:
        """Continuously claim and process jobs, sleeping when idle."""
        while not self._stop.is_set():
            try:
                processed = await self.process_one()
            except Exception as exc:  # noqa: BLE001
                logger.exception("claim loop iteration crashed: %s", exc)
                processed = False
            if not processed:
                # Queue empty — short wait before next poll. The wait
                # is interruptible by `stop()` via the Event.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=_CLAIM_POLL_IDLE_SECONDS)
                except TimeoutError:
                    pass

    async def _reclaim_loop(self) -> None:
        """Periodically reclaim expired leases."""
        interval = self._settings.reclaim_interval_seconds
        while not self._stop.is_set():
            try:
                await self.reclaim_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("reclaim loop iteration crashed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    # ---------- failure handling (HLD-001 §10) ----------

    @staticmethod
    def _error_from_exception(exc: BaseException) -> JobError:
        """Translate a pipeline exception into a `JobError`.

        `PipelineError` carries its own `code` and `retryable` flag.
        Any other exception is treated as a transient infrastructure
        failure (retryable, code `PIPELINE_TRANSIENT`) — we assume
        the failure is recoverable unless the pipeline explicitly
        said otherwise.
        """
        if isinstance(exc, PipelineError):
            return JobError(
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
            )
        return JobError(
            code="PIPELINE_TRANSIENT",
            message=f"{type(exc).__name__}: {exc}",
            retryable=True,
        )

    async def _handle_pipeline_failure(
        self,
        job: Job,
        error: JobError,
    ) -> None:
        """Decide between defer_retry and mark_failed (HLD-001 §10).

        Retryable + attempt < max_attempts → defer with backoff.
        Anything else → terminal FAILED.
        """
        assert job.lease_token is not None

        if error.retryable and job.attempt < self._settings.max_attempts:
            next_retry_at = datetime.now(UTC) + timedelta(
                seconds=self._settings.retry_backoff_seconds,
            )
            deferred = await self._store.defer_retry(
                job.job_id,
                lease_token=job.lease_token,
                next_retry_at=next_retry_at,
            )
            if deferred:
                logger.warning(
                    "job deferred for retry",
                    extra={
                        "job_id": job.job_id,
                        "attempt": job.attempt,
                        "next_retry_at": next_retry_at.isoformat(),
                        "error_code": error.code,
                    },
                )
                return
            # defer_retry lost the lease (rare race) — fall through to
            # mark_failed so the job doesn't get stuck in PROCESSING.
            logger.warning(
                "defer_retry lost the lease, marking failed",
                extra={"job_id": job.job_id},
            )

        await self._store.mark_failed(
            job.job_id,
            lease_token=job.lease_token,
            error=error,
        )
        logger.warning(
            "job failed",
            extra={"job_id": job.job_id, "error_code": error.code},
        )


__all__ = ["Worker"]
