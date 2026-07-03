"""Async SQLite job store.

All operations are atomic. Lease-based claim ensures single-flight
processing — see HLD-001 Section 8.

The store is intentionally connection-per-operation: SQLite's
write-lock semantics guarantee correctness even with multiple
processes, and avoiding a long-lived connection simplifies crash
recovery (no pool to clean up on worker death).
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import aiosqlite

from local_transcription_service.models import Job, JobError, JobStatus

logger = logging.getLogger(__name__)

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    video_url       TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 0,
    lease_token     TEXT,
    lease_expires_at TEXT,
    next_retry_at   TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    transcript_path TEXT,
    error_code      TEXT,
    error_message   TEXT,
    error_retryable INTEGER
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_lease
    ON jobs(status, lease_expires_at);
"""


class JobStore:
    """Async SQLite-backed job queue."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def init(self) -> None:
        """Open the database and apply schema. Idempotent.

        Performs an in-place `ALTER TABLE ADD COLUMN` for columns
        added after the initial schema. Each add is guarded by a
        `PRAGMA table_info` check so it's safe to call on both
        fresh and pre-existing databases.
        """
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            # Idempotent column adds for older DBs.
            async with db.execute("PRAGMA table_info(jobs)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            if "next_retry_at" not in cols:
                await db.execute("ALTER TABLE jobs ADD COLUMN next_retry_at TEXT")
            await db.commit()

    # ---------- submission ----------

    async def submit(self, video_url: str, *, job_id: str | None = None) -> Job:
        """Insert a new job in QUEUED state.

        `job_id` is auto-generated as a URL-safe random token if not
        provided.
        """
        job_id = job_id or secrets.token_urlsafe(16)
        now = datetime.now(UTC)
        job = Job(
            job_id=job_id,
            video_url=video_url,
            status=JobStatus.QUEUED,
            attempt=0,
            created_at=now,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO jobs (job_id, video_url, status, attempt, created_at)
                VALUES (:job_id, :video_url, :status, :attempt, :created_at)
                """,
                {
                    "job_id": job.job_id,
                    "video_url": job.video_url,
                    "status": job.status.value,
                    "attempt": job.attempt,
                    "created_at": _iso(job.created_at),
                },
            )
            await db.commit()
        return job

    # ---------- worker entry points ----------

    async def claim(
        self,
        *,
        lease_ttl_seconds: int = 600,
        lease_token: str | None = None,
    ) -> Job | None:
        """Atomically claim the oldest QUEUED job whose retry time has arrived.

        Returns the claimed Job (with `lease_token` and
        `lease_expires_at` populated), or None if the queue is empty
        or every queued job is still waiting for its `next_retry_at`.

        A single UPDATE...WHERE subquery makes the claim atomic at
        the SQLite statement level — two concurrent claims cannot
        both succeed on the same job because the WHERE clause
        requires `status = 'queued'`.

        Ordering: jobs without a scheduled retry come first (they
        have been waiting longer in absolute terms); deferred jobs
        come after, in `next_retry_at` order.
        """
        lease_token = lease_token or secrets.token_urlsafe(16)
        expires_at = datetime.now(UTC) + timedelta(seconds=lease_ttl_seconds)
        now_iso = _iso(datetime.now(UTC))
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                UPDATE jobs
                SET status = 'claimed',
                    lease_token = ?,
                    lease_expires_at = ?,
                    attempt = attempt + 1
                WHERE job_id = (
                    SELECT job_id FROM jobs
                    WHERE status = 'queued'
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    ORDER BY
                      CASE WHEN next_retry_at IS NULL THEN 0 ELSE 1 END,
                      COALESCE(next_retry_at, created_at),
                      created_at
                    LIMIT 1
                )
                AND status = 'queued'
                RETURNING job_id
                """,
                (lease_token, _iso(expires_at), now_iso),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
        if row is None:
            return None
        return await self.get(row["job_id"])

    async def mark_processing(self, job_id: str, *, lease_token: str) -> bool:
        """Transition CLAIMED -> PROCESSING.

        Only succeeds if the job is currently CLAIMED by `lease_token`.
        Returns True on success.
        """
        now = datetime.now(UTC)
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                UPDATE jobs
                SET status = 'processing', started_at = :started
                WHERE job_id = :job_id
                  AND status = 'claimed'
                  AND lease_token = :token
                """,
                {
                    "started": _iso(now),
                    "job_id": job_id,
                    "token": lease_token,
                },
            )
            await db.commit()
            return cursor.rowcount > 0

    async def mark_done(
        self, job_id: str, *, lease_token: str, transcript_path: str
    ) -> bool:
        """Transition PROCESSING -> DONE.

        Only succeeds if the lease_token matches (defends against a
        late callback from a worker whose lease was reclaimed).
        """
        now = datetime.now(UTC)
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                UPDATE jobs
                SET status = 'done',
                    finished_at = :finished,
                    transcript_path = :path,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE job_id = :job_id
                  AND status = 'processing'
                  AND lease_token = :token
                """,
                {
                    "finished": _iso(now),
                    "path": transcript_path,
                    "job_id": job_id,
                    "token": lease_token,
                },
            )
            await db.commit()
            return cursor.rowcount > 0

    async def mark_failed(
        self, job_id: str, *, lease_token: str, error: JobError
    ) -> bool:
        """Transition PROCESSING|CLAIMED -> FAILED (terminal).

        Sets error fields, clears the lease. Idempotent: if called
        twice (e.g., from a stale worker), only the first call with
        the matching lease_token succeeds.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    finished_at = :finished,
                    error_code = :code,
                    error_message = :message,
                    error_retryable = :retryable,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE job_id = :job_id
                  AND status IN ('processing', 'claimed')
                  AND lease_token = :token
                """,
                {
                    "finished": _iso(datetime.now(UTC)),
                    "code": error.code,
                    "message": error.message,
                    "retryable": int(error.retryable),
                    "job_id": job_id,
                    "token": lease_token,
                },
            )
            await db.commit()
            return cursor.rowcount > 0

    async def defer_retry(
        self,
        job_id: str,
        *,
        lease_token: str,
        next_retry_at: datetime,
    ) -> bool:
        """Move a failed-but-retryable job back to QUEUED with a backoff.

        Used by the worker when a `retryable` pipeline exception is
        raised and `attempt < max_attempts` (HLD-001 §10). The job
        stays in QUEUED with `next_retry_at` set; `claim()` will
        skip it until that time arrives. The lease is released.

        Only succeeds if the job is currently CLAIMED or PROCESSING
        and `lease_token` matches — same stale-worker protection
        as the other transitions.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                UPDATE jobs
                SET status = 'queued',
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    next_retry_at = :next_retry_at
                WHERE job_id = :job_id
                  AND status IN ('processing', 'claimed')
                  AND lease_token = :token
                """,
                {
                    "next_retry_at": _iso(next_retry_at),
                    "job_id": job_id,
                    "token": lease_token,
                },
            )
            await db.commit()
            return cursor.rowcount > 0

    # ---------- background reclaim ----------

    async def reclaim_expired(self) -> int:
        """Return expired CLAIMED/PROCESSING jobs to QUEUED.

        Returns the count of jobs reclaimed. `attempt` is NOT
        incremented here — a reclaimed job's attempt counter still
        reflects the number of times a worker started on it, which
        is what `max_attempts` bounds (HLD-001 §10).
        """
        now_iso = _iso(datetime.now(UTC))
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                UPDATE jobs
                SET status = 'queued',
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE status IN ('claimed', 'processing')
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < ?
                """,
                (now_iso,),
            )
            await db.commit()
            return cursor.rowcount

    # ---------- health probes ----------

    async def ping_writable(self) -> bool:
        """Verify the DB file is openable in write mode (HLD-001 §8).

        Issues `BEGIN IMMEDIATE` to force SQLite to acquire the
        write lock immediately, then `COMMIT` to release it without
        touching any rows. Fails on:

        - Read-only files (e.g., `chmod 0444`, `attrib +R` on Windows).
        - Read-only filesystems (e.g., mounted RO).
        - Permission errors preventing the worker process from
          opening the file in write mode.

        The probe opens a new connection rather than reusing any
        internal state — the store is connection-per-op by design,
        so this is the natural place for a write-capability check.
        """
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - any failure means not writable
            logger.warning("store: ping_writable failed: %s", exc)
            return False
        return True

    # ---------- read-only queries ----------

    async def get(self, job_id: str) -> Job | None:
        """Fetch a job by id, or None if not found."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return Job.from_row(dict(row))

    async def list_by_status(
        self,
        status: JobStatus,
        *,
        limit: int = 100,
    ) -> list[Job]:
        """Fetch up to `limit` jobs in the given status, oldest first."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM jobs WHERE status = ? "
                "ORDER BY created_at ASC LIMIT ?",
                (status.value, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [Job.from_row(dict(r)) for r in rows]

    async def count_by_status(self, status: JobStatus) -> int:
        """Count jobs in a given status."""
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = ?",
                (status.value,),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0


def _iso(dt: datetime | None) -> str | None:
    """Serialize datetime to ISO 8601 with UTC tzinfo.

    Naive datetimes are assumed UTC. Always produces a value that
    round-trips through `datetime.fromisoformat()` preserving tzinfo.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()