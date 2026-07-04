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

Multi-worker (HLD-001 §5 amended 2026-07-04, Phase D): the
``worker_count`` constructor argument controls how many claim
loops run cooperatively in the same event loop. Each claim task
gets a stable ``worker_id`` (``f"w{i}"``) in structured log
events so operators can correlate per-task activity. The
reclaim loop stays single — it is already idempotent and cheap.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from local_transcription_service.config import Settings
from local_transcription_service.metrics import ErrorRateCounter, run_error_rate_loop
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
        *,
        on_done: Callable[[str], None] | None = None,
        worker_count: int = 1,
        error_rate_counter: ErrorRateCounter | None = None,
    ) -> None:
        """Construct the worker.

        ``on_done`` is an OPTIONAL test-only signal hook invoked
        synchronously after a job successfully transitions to
        ``DONE`` (i.e. after ``store.mark_done`` returns truthy).
        Production callers omit it; the hook is a no-op by default
        and adds zero overhead to the hot path beyond a single
        ``is None`` check.

        The hook is fired with the just-completed ``job_id``. It is
        intentionally a plain callable (not a coroutine) so the
        worker can stay in the synchronous end-of-iteration code
        path. If a test needs to fan out asynchronously, it can
        schedule work from inside the callback.

        ``worker_count`` (Phase D, HLD-001 §5 amended) controls how
        many claim loops run cooperatively in the same event loop.
        Default ``1`` preserves single-worker behaviour. Values
        outside ``[1, 64]`` are rejected by ``Settings.worker_count``
        via pydantic ``Field(ge=1, le=64)``.

        ``error_rate_counter`` (Phase D, HLD-001 §15.1) is an optional
        counter that is incremented on every terminal FAIL. Pass
        ``None`` (default) to skip the metric; production wires
        ``app.main()`` to pass a real instance.
        """
        self._store = store
        self._pipeline = pipeline
        self._settings = settings
        self._stop = asyncio.Event()
        self._on_done = on_done
        self._worker_count = worker_count
        self._error_rate_counter = error_rate_counter

    # ---------- lifecycle ----------

    def stop(self) -> None:
        """Signal the run loop to exit on the next iteration."""
        self._stop.set()

    async def run_forever(self) -> None:
        """Run claim and reclaim loops until `stop()` is called.

        Multi-worker (Phase D, HLD-001 §5 amended): spawns
        ``worker_count`` claim tasks cooperatively in the same event
        loop. Each task gets a stable ``worker_id`` (``f"w{i}"``) in
        its structured log events. The reclaim loop stays single.

        If an ``error_rate_counter`` was passed at construction, a
        third task runs the 60-second tick loop in parallel.
        """
        claim_tasks = [
            asyncio.create_task(self._claim_loop(worker_id=f"w{i}"), name=f"lts-claim-{i}")
            for i in range(self._worker_count)
        ]
        reclaim_task = asyncio.create_task(self._reclaim_loop(), name="lts-reclaim")
        tasks = [*claim_tasks, reclaim_task]

        rate_task: asyncio.Task[None] | None = None
        if self._error_rate_counter is not None:
            rate_task = asyncio.create_task(
                run_error_rate_loop(
                    self._error_rate_counter,
                    stop_event=self._stop,
                ),
                name="lts-error-rate",
            )
            tasks.append(rate_task)

        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---------- single-iteration entry points (test-friendly) ----------

    async def process_one(self, *, worker_id: str = "w0") -> bool:
        """Process at most one job. Returns True if a job was processed.

        Respects `max_attempts`: if the just-claimed job's `attempt`
        counter has already exceeded the limit, it is marked FAILED
        with code `MAX_ATTEMPTS` and not re-run.

        ``worker_id`` (Phase D) is included in every structured log
        event for this iteration. Default ``"w0"`` preserves the
        single-worker log shape; multi-worker tasks pass
        ``f"w{i}"`` for ``i`` in ``range(worker_count)``.
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
            logger.info(
                "job failed: max attempts",
                extra={"job_id": job.job_id, "worker_id": worker_id},
            )
            if self._error_rate_counter is not None:
                self._error_rate_counter.increment("MAX_ATTEMPTS")
            return True

        moved = await self._store.mark_processing(
            job.job_id,
            lease_token=job.lease_token,
        )
        if not moved:
            logger.info(
                "claim lost before processing",
                extra={"job_id": job.job_id, "worker_id": worker_id},
            )
            return True

        try:
            text = await self._pipeline.transcribe(job.video_url, job_id=job.job_id)
        except Exception as exc:  # noqa: BLE001 - any pipeline failure is a job failure
            error = self._error_from_exception(exc)
            await self._handle_pipeline_failure(job, error, worker_id=worker_id)
            return True

        # HLD-001 §11 / §13: results are written as `.md` (not `.txt`).
        # The extension is the operator-facing contract; ``/jobs/{id}/result``
        # streams the file with ``text/plain; charset=utf-8`` regardless.
        transcript_path = self._settings.results_dir / f"{job.job_id}.md"
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
                extra={"job_id": job.job_id, "worker_id": worker_id},
            )
        else:
            logger.info(
                "job done",
                extra={"job_id": job.job_id, "worker_id": worker_id},
            )
            if self._on_done is not None:
                self._on_done(job.job_id)
        return True

    async def reclaim_once(self) -> int:
        """One reclaim pass. Returns the number of jobs returned to QUEUED."""
        n = await self._store.reclaim_expired()
        if n:
            logger.info("reclaimed %d expired job(s)", n)
        return n

    # ---------- long-running loops ----------

    async def _claim_loop(self, *, worker_id: str = "w0") -> None:
        """Continuously claim and process jobs, sleeping when idle.

        ``worker_id`` is threaded through to :meth:`process_one` so
        every structured log event in this iteration carries the
        caller's stable identifier (Phase D, HLD §5).
        """
        while not self._stop.is_set():
            try:
                processed = await self.process_one(worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "claim loop iteration crashed: %s",
                    exc,
                    extra={"worker_id": worker_id},
                )
                processed = False
            if not processed:
                # Queue empty — short wait before next poll. The wait
                # is interruptible by `stop()` via the Event.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=_CLAIM_POLL_IDLE_SECONDS)
                except TimeoutError:
                    pass

    async def _reclaim_loop(self) -> None:
        """Periodically reclaim expired leases.

        Stays single even under ``worker_count > 1`` (HLD §5 amended):
        the SQL update is atomic at the SQLite statement level, so
        two reclaim loops would race for the same row only to have
        one of them no-op. One loop is cheaper and easier to reason
        about.
        """
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
        *,
        worker_id: str = "w0",
    ) -> None:
        """Decide between defer_retry and mark_failed (HLD-001 §10).

        Retryable + attempt < max_attempts → defer with backoff.
        Anything else → terminal FAILED.

        ``worker_id`` is threaded into every structured log event
        here. The error-rate counter is incremented on the FAIL
        path (not the defer path) — only terminal failures count
        for the rate metric.
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
                        "worker_id": worker_id,
                    },
                )
                return
            # defer_retry lost the lease (rare race) — fall through to
            # mark_failed so the job doesn't get stuck in PROCESSING.
            logger.warning(
                "defer_retry lost the lease, marking failed",
                extra={"job_id": job.job_id, "worker_id": worker_id},
            )

        await self._store.mark_failed(
            job.job_id,
            lease_token=job.lease_token,
            error=error,
        )
        logger.warning(
            "job failed",
            extra={
                "job_id": job.job_id,
                "error_code": error.code,
                "worker_id": worker_id,
            },
        )
        if self._error_rate_counter is not None:
            self._error_rate_counter.increment(error.code)


__all__ = ["Worker"]
