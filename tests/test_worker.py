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
