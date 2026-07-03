"""Tests for the JobStore (queue/store.py).

Uses per-test temporary SQLite files via pytest's `tmp_path`
fixture — in-memory SQLite (`:memory:`) is per-connection in
aiosqlite, so it does not survive across the per-operation
connections this store opens.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest_asyncio

from local_transcription_service.models import JobError, JobStatus
from local_transcription_service.queue.store import JobStore

# ---------- fixtures ----------


@pytest_asyncio.fixture
async def store(tmp_path) -> JobStore:
    """A fresh JobStore backed by a per-test SQLite file."""
    s = JobStore(tmp_path / "jobs.db")
    await s.init()
    return s


# ---------- init / schema ----------


async def test_init_is_idempotent(store: JobStore) -> None:
    """Schema is applied idempotently — re-running init must not raise."""
    await store.init()
    job = await store.submit("https://example.com/watch?v=abc")
    assert job.job_id


# ---------- submit ----------


async def test_submit_creates_queued_job(store: JobStore) -> None:
    job = await store.submit("https://example.com/watch?v=abc")
    assert job.status == JobStatus.QUEUED
    assert job.attempt == 0
    assert job.video_url == "https://example.com/watch?v=abc"
    assert job.created_at.tzinfo is not None
    assert job.lease_token is None
    assert job.lease_expires_at is None


async def test_submit_assigns_unique_job_ids(store: JobStore) -> None:
    j1 = await store.submit("https://example.com/watch?v=a")
    j2 = await store.submit("https://example.com/watch?v=b")
    assert j1.job_id != j2.job_id


async def test_submit_accepts_explicit_job_id(store: JobStore) -> None:
    await store.submit("https://example.com/watch?v=abc", job_id="my-id-1")
    fetched = await store.get("my-id-1")
    assert fetched is not None
    assert fetched.job_id == "my-id-1"


# ---------- claim ----------


async def test_claim_returns_oldest_queued_first(store: JobStore) -> None:
    j1 = await store.submit("https://example.com/watch?v=1")
    await store.submit("https://example.com/watch?v=2")
    claimed = await store.claim(lease_ttl_seconds=600)
    assert claimed is not None
    assert claimed.job_id == j1.job_id


async def test_claim_returns_none_when_empty(store: JobStore) -> None:
    assert await store.claim() is None


async def test_claim_increments_attempt_and_sets_lease(store: JobStore) -> None:
    await store.submit("https://example.com/watch?v=1")
    claimed = await store.claim(lease_ttl_seconds=600, lease_token="test-token")
    assert claimed is not None
    assert claimed.attempt == 1
    assert claimed.lease_token == "test-token"
    assert claimed.lease_expires_at is not None


async def test_claim_does_not_pick_already_claimed_job(store: JobStore) -> None:
    await store.submit("https://example.com/watch?v=1")
    j2 = await store.submit("https://example.com/watch?v=2")
    first = await store.claim()
    assert first is not None
    second = await store.claim()
    assert second is not None
    assert second.job_id == j2.job_id
    assert first.job_id != second.job_id


async def test_claim_auto_generates_lease_token(store: JobStore) -> None:
    await store.submit("https://example.com/watch?v=1")
    claimed = await store.claim()
    assert claimed is not None
    assert claimed.lease_token is not None
    assert len(claimed.lease_token) > 0


# ---------- mark_processing / mark_done / mark_failed ----------


async def test_mark_processing_requires_correct_lease_token(store: JobStore) -> None:
    await store.submit("https://example.com/watch?v=1")
    claimed = await store.claim()
    assert claimed is not None
    token = claimed.lease_token or ""

    # Wrong token must fail.
    assert await store.mark_processing(claimed.job_id, lease_token="wrong") is False

    # Right token succeeds.
    assert await store.mark_processing(claimed.job_id, lease_token=token) is True

    refreshed = await store.get(claimed.job_id)
    assert refreshed is not None
    assert refreshed.status == JobStatus.PROCESSING
    assert refreshed.started_at is not None


async def test_mark_done_terminal_success(store: JobStore) -> None:
    await store.submit("https://example.com/watch?v=1")
    claimed = await store.claim()
    assert claimed is not None
    token = claimed.lease_token or ""
    await store.mark_processing(claimed.job_id, lease_token=token)

    ok = await store.mark_done(
        claimed.job_id,
        lease_token=token,
        transcript_path="/tmp/results/abc.md",
    )
    assert ok is True

    refreshed = await store.get(claimed.job_id)
    assert refreshed is not None
    assert refreshed.status == JobStatus.DONE
    assert refreshed.transcript_path == "/tmp/results/abc.md"
    assert refreshed.finished_at is not None
    # Lease fields are cleared on terminal transition.
    assert refreshed.lease_token is None
    assert refreshed.lease_expires_at is None


async def test_mark_failed_terminal_with_error(store: JobStore) -> None:
    await store.submit("https://example.com/watch?v=1")
    claimed = await store.claim()
    assert claimed is not None
    token = claimed.lease_token or ""
    await store.mark_processing(claimed.job_id, lease_token=token)

    ok = await store.mark_failed(
        claimed.job_id,
        lease_token=token,
        error=JobError(code="FETCH_FAILED", message="yt-dlp exited 1", retryable=True),
    )
    assert ok is True

    refreshed = await store.get(claimed.job_id)
    assert refreshed is not None
    assert refreshed.status == JobStatus.FAILED
    assert refreshed.error is not None
    assert refreshed.error.code == "FETCH_FAILED"
    assert refreshed.error.retryable is True
    assert refreshed.finished_at is not None
    # Lease cleared on terminal.
    assert refreshed.lease_token is None


async def test_mark_done_after_reclaim_is_rejected(store: JobStore) -> None:
    """A late mark_done from a worker whose lease was reclaimed must not succeed."""
    await store.submit("https://example.com/watch?v=1")
    first = await store.claim(lease_ttl_seconds=1)
    assert first is not None
    first_token = first.lease_token or ""

    # Simulate lease expiration + reclaim + new claim.
    await asyncio.sleep(1.1)
    assert await store.reclaim_expired() == 1

    second = await store.claim()
    assert second is not None
    assert second.job_id == first.job_id
    assert second.attempt == 2  # bumped on second claim

    # The first worker's late mark_done must NOT clobber the new state.
    ok = await store.mark_done(
        first.job_id,
        lease_token=first_token,
        transcript_path="/stale/path.md",
    )
    assert ok is False

    refreshed = await store.get(first.job_id)
    assert refreshed is not None
    assert refreshed.status == JobStatus.CLAIMED  # second worker now owns it
    assert refreshed.transcript_path is None


# ---------- reclaim_expired ----------


async def test_reclaim_returns_expired_claimed_jobs_to_queued(store: JobStore) -> None:
    await store.submit("https://example.com/watch?v=1")
    await store.claim(lease_ttl_seconds=1)
    await asyncio.sleep(1.1)
    count = await store.reclaim_expired()
    assert count == 1
    queued = await store.list_by_status(JobStatus.QUEUED)
    assert len(queued) == 1


async def test_reclaim_does_not_touch_unexpired(store: JobStore) -> None:
    await store.submit("https://example.com/watch?v=1")
    claimed = await store.claim(lease_ttl_seconds=600)
    assert claimed is not None
    assert await store.reclaim_expired() == 0
    refreshed = await store.get(claimed.job_id)
    assert refreshed is not None
    assert refreshed.status == JobStatus.CLAIMED


async def test_reclaim_does_not_increment_attempt(store: JobStore) -> None:
    """attempt reflects times a worker started; reclaim doesn't bump it."""
    await store.submit("https://example.com/watch?v=1")
    first = await store.claim(lease_ttl_seconds=1)
    assert first is not None
    await asyncio.sleep(1.1)
    await store.reclaim_expired()
    second = await store.claim()
    assert second is not None
    # First claim bumped to 1. Reclaim did not bump. Second claim bumped to 2.
    assert second.attempt == 2


async def test_reclaim_ignores_terminal_jobs(store: JobStore) -> None:
    """DONE and FAILED jobs must never be reclaimed."""
    await store.submit("https://example.com/watch?v=1")
    claimed = await store.claim(lease_ttl_seconds=1)
    assert claimed is not None
    token = claimed.lease_token or ""
    await store.mark_processing(claimed.job_id, lease_token=token)
    await store.mark_done(
        claimed.job_id,
        lease_token=token,
        transcript_path="/tmp/x.md",
    )
    await asyncio.sleep(1.1)
    # Lease was cleared by mark_done; even if it weren't, reclaim should skip.
    assert await store.reclaim_expired() == 0


# ---------- read-only queries ----------


async def test_get_returns_none_for_unknown(store: JobStore) -> None:
    assert await store.get("does-not-exist") is None


async def test_count_by_status(store: JobStore) -> None:
    assert await store.count_by_status(JobStatus.QUEUED) == 0
    await store.submit("https://example.com/watch?v=1")
    await store.submit("https://example.com/watch?v=2")
    assert await store.count_by_status(JobStatus.QUEUED) == 2
    await store.claim()
    assert await store.count_by_status(JobStatus.QUEUED) == 1
    assert await store.count_by_status(JobStatus.CLAIMED) == 1


# ---------- defer_retry (HLD-001 §10) ----------


async def test_defer_retry_moves_claimed_job_back_to_queued_with_retry_at(
    store: JobStore,
) -> None:
    job = await store.submit("https://example.com/watch?v=retry")
    claimed = await store.claim()
    assert claimed is not None
    assert claimed.job_id == job.job_id
    token = claimed.lease_token or ""
    await store.mark_processing(claimed.job_id, lease_token=token)

    next_retry = datetime.now(UTC) + timedelta(seconds=30)
    ok = await store.defer_retry(
        claimed.job_id,
        lease_token=token,
        next_retry_at=next_retry,
    )
    assert ok is True

    refreshed = await store.get(claimed.job_id)
    assert refreshed is not None
    assert refreshed.status == JobStatus.QUEUED
    assert refreshed.lease_token is None
    assert refreshed.lease_expires_at is None
    assert refreshed.next_retry_at is not None


async def test_defer_retry_requires_correct_lease_token(store: JobStore) -> None:
    await store.submit("https://example.com/watch?v=x")
    claimed = await store.claim()
    assert claimed is not None
    token = claimed.lease_token or ""
    await store.mark_processing(claimed.job_id, lease_token=token)

    ok = await store.defer_retry(
        claimed.job_id,
        lease_token="wrong-token",
        next_retry_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    assert ok is False

    refreshed = await store.get(claimed.job_id)
    assert refreshed is not None
    # The job is still in PROCESSING because defer_retry was rejected.
    assert refreshed.status == JobStatus.PROCESSING


async def test_defer_retry_from_claimed_state(store: JobStore) -> None:
    """defer_retry works on CLAIMED (not just PROCESSING) jobs."""
    await store.submit("https://example.com/watch?v=claimed")
    claimed = await store.claim()
    assert claimed is not None
    token = claimed.lease_token or ""
    # Skip mark_processing — go straight to defer_retry from CLAIMED.
    ok = await store.defer_retry(
        claimed.job_id,
        lease_token=token,
        next_retry_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    assert ok is True
    refreshed = await store.get(claimed.job_id)
    assert refreshed is not None
    assert refreshed.status == JobStatus.QUEUED


# ---------- claim with next_retry_at filter ----------


async def test_claim_skips_jobs_with_future_next_retry_at(store: JobStore) -> None:
    # Submit two jobs; defer the first so it can't be claimed yet.
    j_deferred = await store.submit("https://example.com/watch?v=later")
    j_immediate = await store.submit("https://example.com/watch?v=now")

    first = await store.claim()
    assert first is not None
    assert first.job_id == j_deferred.job_id
    token = first.lease_token or ""
    future = datetime.now(UTC) + timedelta(hours=1)
    await store.defer_retry(first.job_id, lease_token=token, next_retry_at=future)

    # Second claim must skip the deferred job and return the immediate one.
    second = await store.claim()
    assert second is not None
    assert second.job_id == j_immediate.job_id


async def test_claim_returns_deferred_job_after_next_retry_at(store: JobStore) -> None:
    job = await store.submit("https://example.com/watch?v=retry")
    claimed = await store.claim()
    assert claimed is not None
    token = claimed.lease_token or ""
    # Defer to 1 second in the past so it's immediately claimable again.
    past = datetime.now(UTC) - timedelta(seconds=1)
    await store.defer_retry(claimed.job_id, lease_token=token, next_retry_at=past)

    second = await store.claim()
    assert second is not None
    assert second.job_id == job.job_id
    # attempt is bumped on each claim, not by defer_retry.
    assert second.attempt == 2


async def test_init_adds_next_retry_at_column_to_pre_existing_db(
    tmp_path,
) -> None:
    """`init()` must add the `next_retry_at` column to a DB created by
    a previous version of the code (idempotent migration)."""
    import aiosqlite

    db_path = tmp_path / "legacy.db"
    # Create a "legacy" DB without next_retry_at.
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                video_url TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                transcript_path TEXT,
                error_code TEXT,
                error_message TEXT,
                error_retryable INTEGER
            )
            """
        )
        await db.commit()

    # Now run init() — it must add the missing column.
    store = JobStore(db_path)
    await store.init()

    async with aiosqlite.connect(str(db_path)) as db:
        async with db.execute("PRAGMA table_info(jobs)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
    assert "next_retry_at" in cols


# ---------- ping_writable (HLD-001 §8 / /ready) ----------


async def test_ping_writable_returns_true_for_writable_db(store: JobStore) -> None:
    """A real tmp_path store must report itself as writable."""
    assert await store.ping_writable() is True


async def test_ping_writable_returns_false_when_path_is_directory(
    tmp_path,
) -> None:
    """A path that points to a directory (not a file) cannot be opened
    as a SQLite DB — the write probe must return False."""
    bad = tmp_path / "not-a-file"
    bad.mkdir()
    store = JobStore(bad)
    # Do NOT call init() — we want to test the raw open behavior.
    assert await store.ping_writable() is False