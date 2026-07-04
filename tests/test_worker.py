"""Tests for the worker claim and reclaim logic (HLD-001 §10)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from local_transcription_service.config import Settings
from local_transcription_service.models import JobStatus
from local_transcription_service.pipeline.base import MockPipeline, PipelineError
from local_transcription_service.queue.store import JobStore
from local_transcription_service.worker import Worker

# ---------- process_one: success path ----------


async def test_process_one_runs_mock_pipeline_and_marks_done(
    settings: Settings, store: JobStore
) -> None:
    worker = Worker(store, MockPipeline(), settings)
    job = await store.submit("https://www.youtube.com/watch?v=video1")
    processed = await worker.process_one()

    assert processed is True
    after = await store.get(job.job_id)
    assert after is not None
    assert after.status == JobStatus.DONE
    assert after.attempt == 1
    assert after.transcript_path is not None
    transcript_text = Path(after.transcript_path).read_text(encoding="utf-8")  # noqa: ASYNC240
    expected = (
        f"mock transcript for https://www.youtube.com/watch?v=video1 "
        f"(job_id={job.job_id})\n"
    )
    assert transcript_text == expected


async def test_process_one_returns_false_when_queue_empty(
    settings: Settings, store: JobStore
) -> None:
    worker = Worker(store, MockPipeline(), settings)
    assert await worker.process_one() is False


# ---------- process_one: failure handling (HLD-001 §10) ----------


async def test_process_one_defers_retry_on_retryable_error(
    settings: Settings, store: JobStore
) -> None:
    """Retryable pipeline failures schedule a retry with backoff."""

    class TransientFailure(MockPipeline):
        async def transcribe(self, video_url: str, *, job_id: str) -> str:
            raise PipelineError("ollama disconnected", retryable=True)

    worker = Worker(store, TransientFailure(), settings)
    job = await store.submit("https://www.youtube.com/watch?v=transient")
    await worker.process_one()

    after = await store.get(job.job_id)
    assert after is not None
    assert after.status == JobStatus.QUEUED
    assert after.attempt == 1  # claim bumped it
    assert after.next_retry_at is not None
    assert after.next_retry_at > datetime.now(UTC) - timedelta(seconds=1)
    # Backoff is approximately retry_backoff_seconds (30s by default).
    delta = (after.next_retry_at - datetime.now(UTC)).total_seconds()
    assert 0 < delta <= settings.retry_backoff_seconds + 1


async def test_process_one_marks_failed_on_non_retryable_error(
    settings: Settings, store: JobStore
) -> None:
    """Non-retryable pipeline failures go straight to FAILED."""

    class FatalFailure(MockPipeline):
        async def transcribe(self, video_url: str, *, job_id: str) -> str:
            raise PipelineError(
                "video unavailable", code="VIDEO_UNAVAILABLE", retryable=False
            )

    worker = Worker(store, FatalFailure(), settings)
    job = await store.submit("https://www.youtube.com/watch?v=gone")
    await worker.process_one()

    after = await store.get(job.job_id)
    assert after is not None
    assert after.status == JobStatus.FAILED
    assert after.error is not None
    assert after.error.code == "VIDEO_UNAVAILABLE"
    assert after.error.retryable is False
    assert after.next_retry_at is None


async def test_process_one_treats_untyped_exception_as_retryable(
    settings: Settings, store: JobStore
) -> None:
    """A bare `RuntimeError` (no PipelineError) is treated as transient
    by default — defensive choice from HLD-001 §10."""

    class BoomPipeline(MockPipeline):
        async def transcribe(self, video_url: str, *, job_id: str) -> str:
            raise RuntimeError("kaboom")

    worker = Worker(store, BoomPipeline(), settings)
    job = await store.submit("https://www.youtube.com/watch?v=boom")
    await worker.process_one()

    after = await store.get(job.job_id)
    assert after is not None
    assert after.status == JobStatus.QUEUED  # deferred
    assert after.next_retry_at is not None


async def test_process_one_marks_failed_after_retryable_exhausts_attempts(
    settings: Settings, store: JobStore
) -> None:
    """With max_attempts=2, the 2nd retryable failure is terminal."""
    settings = Settings(
        auth_token=settings.auth_token,
        data_dir=settings.data_dir,
        lease_ttl_seconds=settings.lease_ttl_seconds,
        reclaim_interval_seconds=settings.reclaim_interval_seconds,
        max_attempts=2,
        retry_backoff_seconds=0,  # speed up the test
        stt_engine="mock",  # B5a: avoid openai-requires-api-key validator
    )

    class TransientFailure(MockPipeline):
        async def transcribe(self, video_url: str, *, job_id: str) -> str:
            raise PipelineError("flaky", retryable=True)

    worker = Worker(store, TransientFailure(), settings)
    job = await store.submit("https://www.youtube.com/watch?v=exhaust")

    # First processing attempt: retryable → deferred.
    await worker.process_one()
    after = await store.get(job.job_id)
    assert after is not None
    assert after.status == JobStatus.QUEUED
    assert after.attempt == 1

    # Force next_retry_at into the past so the next claim picks it up.
    async with aiosqlite.connect(str(settings.db_path)) as db:
        await db.execute(
            "UPDATE jobs SET next_retry_at=? WHERE job_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job.job_id),
        )
        await db.commit()

    # Second processing attempt: retryable, but attempt=2 and max=2 → FAILED.
    await worker.process_one()
    after = await store.get(job.job_id)
    assert after is not None
    assert after.status == JobStatus.FAILED
    assert after.error is not None
    # PipelineError("flaky", retryable=True) → code defaults to HLD-canonical
    # FETCH_FAILED (base.py default).
    assert after.error.code == "FETCH_FAILED"
    assert after.error.retryable is True
    assert after.attempt == 2


async def test_process_one_marks_failed_when_max_attempts_exceeded(
    settings: Settings, store: JobStore
) -> None:
    """Defensive check: if a claim somehow bumps attempt past max_attempts,
    the job goes straight to FAILED with MAX_ATTEMPTS code."""
    job = await store.submit("https://www.youtube.com/watch?v=cap")
    # Simulate prior failed attempts: bump to attempt=2 (max=2).
    async with aiosqlite.connect(str(settings.db_path)) as db:
        await db.execute(
            "UPDATE jobs SET status='queued', attempt=2, "
            "lease_token=NULL, lease_expires_at=NULL WHERE job_id=?",
            (job.job_id,),
        )
        await db.commit()

    worker = Worker(store, MockPipeline(), settings)
    processed = await worker.process_one()
    assert processed is True

    after = await store.get(job.job_id)
    assert after is not None
    assert after.status == JobStatus.FAILED
    assert after.error is not None
    assert after.error.code == "MAX_ATTEMPTS"
    assert after.error.retryable is False


# ---------- reclaim ----------


async def test_reclaim_once_returns_expired_claimed_job_to_queued(
    settings: Settings, store: JobStore
) -> None:
    job = await store.submit("https://www.youtube.com/watch?v=expired")
    claimed = await store.claim(lease_ttl_seconds=60)
    assert claimed is not None
    assert claimed.job_id == job.job_id

    past = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    async with aiosqlite.connect(str(settings.db_path)) as db:
        await db.execute(
            "UPDATE jobs SET lease_expires_at=? WHERE job_id=?",
            (past, job.job_id),
        )
        await db.commit()

    worker = Worker(store, MockPipeline(), settings)
    n = await worker.reclaim_once()
    assert n == 1

    after = await store.get(job.job_id)
    assert after is not None
    assert after.status == JobStatus.QUEUED
    assert after.lease_token is None


async def test_reclaim_once_returns_zero_when_nothing_expired(
    settings: Settings, store: JobStore
) -> None:
    await store.submit("https://www.youtube.com/watch?v=fresh")
    worker = Worker(store, MockPipeline(), settings)
    assert await worker.reclaim_once() == 0


# ---------- run_forever ----------


async def test_run_forever_processes_multiple_jobs(
    settings: Settings, store: JobStore
) -> None:
    for i in range(3):
        await store.submit(f"https://www.youtube.com/watch?v=job{i}")

    done_count = 0
    done_event = asyncio.Event()

    def _on_done(job_id: str) -> None:
        nonlocal done_count
        done_count += 1
        if done_count >= 3:
            done_event.set()

    worker = Worker(store, MockPipeline(), settings, on_done=_on_done)

    async def _stop_after_done() -> None:
        # Wait deterministically for the third mark_done callback
        # instead of racing the wall clock — the previous shape
        # (asyncio.sleep(0.2)) was a flake on Windows because
        # SQLite write-lock contention can exceed 200 ms.
        await asyncio.wait_for(done_event.wait(), timeout=5.0)
        worker.stop()

    stop_task = asyncio.create_task(_stop_after_done())
    await asyncio.wait_for(worker.run_forever(), timeout=5.0)
    await stop_task

    assert done_count == 3
    assert await store.count_by_status(JobStatus.DONE) == 3


# ---------- multi-worker (HLD-001 §5 amended, Phase D) ----------


async def test_run_forever_with_worker_count_4_processes_concurrent_jobs(
    settings: Settings, store: JobStore
) -> None:
    """8 jobs + worker_count=4 → all 8 reach DONE; deterministic drain
    via per-job done-event (Phase B6 pattern).

    Pins HLD-001 §5 amended: N claim tasks cooperatively drain the
    queue without double-processing. SQLite's atomic claim
    (`UPDATE ... WHERE status='queued'`) ensures each job is
    processed exactly once even when 4 tasks race.
    """
    n_jobs = 8
    for i in range(n_jobs):
        await store.submit(f"https://www.youtube.com/watch?v=multi{i}")

    done_count = 0
    done_event = asyncio.Event()

    def _on_done(job_id: str) -> None:
        nonlocal done_count
        done_count += 1
        if done_count >= n_jobs:
            done_event.set()

    worker = Worker(
        store,
        MockPipeline(),
        settings,
        on_done=_on_done,
        worker_count=4,
    )

    async def _stop_after_done() -> None:
        await asyncio.wait_for(done_event.wait(), timeout=10.0)
        worker.stop()

    stop_task = asyncio.create_task(_stop_after_done())
    await asyncio.wait_for(worker.run_forever(), timeout=10.0)
    await stop_task

    assert done_count == n_jobs
    assert await store.count_by_status(JobStatus.DONE) == n_jobs


async def test_concurrent_claim_only_one_worker_wins_per_job(
    settings: Settings, store: JobStore
) -> None:
    """10 concurrent claim() calls against a 5-job queue → exactly 5
    succeed, 5 return None.

    Pins the atomic-claim property of the SQL — two concurrent tasks
    cannot both win the same row because the WHERE clause filters
    by `status='queued'` and the SET clause changes the status.
    SQLite's per-statement write-lock serialises the two UPDATEs.
    """
    n_jobs = 5
    for i in range(n_jobs):
        await store.submit(f"https://www.youtube.com/watch?v=claim{i}")

    results = await asyncio.gather(
        *[store.claim(lease_ttl_seconds=60) for _ in range(10)],
        return_exceptions=False,
    )

    succeeded = [r for r in results if r is not None]
    failed = [r for r in results if r is None]

    assert len(succeeded) == n_jobs, f"expected {n_jobs} winners, got {len(succeeded)}"
    assert len(failed) == 10 - n_jobs

    # Each winner has a unique job_id.
    winner_ids = {r.job_id for r in succeeded}
    assert len(winner_ids) == n_jobs


async def test_concurrent_mark_processing_respects_lease(
    settings: Settings, store: JobStore
) -> None:
    """Two tasks each claim a distinct job; both call mark_processing
    with the WRONG lease_token → only the matching lease succeeds.

    Pins the lease-token filter on mark_processing — the "stale
    worker lost the lease" guard that prevents a reclaimed job
    from being advanced by the wrong worker.
    """
    job_a = await store.submit("https://www.youtube.com/watch?v=lease_a")
    job_b = await store.submit("https://www.youtube.com/watch?v=lease_b")

    claimed_a = await store.claim(lease_ttl_seconds=60)
    claimed_b = await store.claim(lease_ttl_seconds=60)
    assert claimed_a is not None and claimed_b is not None
    assert claimed_a.lease_token != claimed_b.lease_token

    # Cross-call: A's worker tries B's job, B's worker tries A's job.
    wrong_a = await store.mark_processing(
        job_b.job_id, lease_token=claimed_a.lease_token,
    )
    wrong_b = await store.mark_processing(
        job_a.job_id, lease_token=claimed_b.lease_token,
    )

    assert wrong_a is False, "wrong lease_token must NOT advance job_b"
    assert wrong_b is False, "wrong lease_token must NOT advance job_a"

    # Now the correct calls succeed.
    right_a = await store.mark_processing(
        job_a.job_id, lease_token=claimed_a.lease_token,
    )
    right_b = await store.mark_processing(
        job_b.job_id, lease_token=claimed_b.lease_token,
    )
    assert right_a is True
    assert right_b is True
