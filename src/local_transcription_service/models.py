"""Domain models for the local transcription service.

Internal types used by the queue, pipeline, and worker. These are
distinct from the API request/response schemas (see api/schemas.py,
to be added in a later phase).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class JobStatus(StrEnum):
    """Job lifecycle states.

    See HLD-001 Section 9 for the full state machine.

    Transitions:
        QUEUED -> CLAIMED       (worker claim, attempt++)
        CLAIMED -> PROCESSING   (worker starts pipeline)
        CLAIMED|PROCESSING -> QUEUED  (lease expired, reclaim)
        PROCESSING -> DONE      (success)
        PROCESSING -> FAILED    (any failure, terminal)
    """

    QUEUED = "queued"
    CLAIMED = "claimed"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """True if no further state transitions are possible."""
        return self in (JobStatus.DONE, JobStatus.FAILED)

    @property
    def is_active(self) -> bool:
        """True if a worker currently holds responsibility for the job."""
        return self in (JobStatus.CLAIMED, JobStatus.PROCESSING)

    @property
    def is_reclaimable(self) -> bool:
        """True if a reclaim scan can return this job to QUEUED."""
        return self in (JobStatus.CLAIMED, JobStatus.PROCESSING)


@dataclass(frozen=True)
class JobError:
    """Structured error attached to a FAILED job."""

    code: str          # e.g. "FETCH_FAILED", "MODEL_NOT_PULLED"
    message: str       # Human-readable description
    retryable: bool    # Hint for whether resubmitting makes sense


@dataclass
class Job:
    """A transcription job in the queue."""

    job_id: str
    video_url: str
    status: JobStatus
    attempt: int
    created_at: datetime

    started_at: datetime | None = None
    finished_at: datetime | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    next_retry_at: datetime | None = None
    transcript_path: str | None = None
    error: JobError | None = None
    acked_at: datetime | None = None  # HLD-001 §13.1

    def to_row(self) -> dict[str, object]:
        """Serialize to a dict matching the SQLite schema in HLD-001 §7."""
        return {
            "job_id": self.job_id,
            "video_url": self.video_url,
            "status": self.status.value,
            "attempt": self.attempt,
            "lease_token": self.lease_token,
            "lease_expires_at": _iso(self.lease_expires_at),
            "next_retry_at": _iso(self.next_retry_at),
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "transcript_path": self.transcript_path,
            "error_code": self.error.code if self.error else None,
            "error_message": self.error.message if self.error else None,
            "error_retryable": int(self.error.retryable) if self.error else None,
            "acked_at": _iso(self.acked_at),
        }

    @classmethod
    def from_row(cls, row: dict[str, object]) -> Job:
        """Deserialize from a SQLite row (dict or aiosqlite.Row)."""
        error: JobError | None = None
        if row["error_code"] is not None:
            error = JobError(
                code=row["error_code"],  # type: ignore[arg-type]
                message=row["error_message"],  # type: ignore[arg-type]
                retryable=bool(row["error_retryable"]),
            )
        return cls(
            job_id=row["job_id"],  # type: ignore[arg-type]
            video_url=row["video_url"],  # type: ignore[arg-type]
            status=JobStatus(row["status"]),  # type: ignore[arg-type]
            attempt=row["attempt"],  # type: ignore[arg-type]
            created_at=_from_iso(row["created_at"]),  # type: ignore[arg-type]
            started_at=_from_iso(row["started_at"]) if row["started_at"] else None,  # type: ignore[arg-type]
            finished_at=_from_iso(row["finished_at"]) if row["finished_at"] else None,  # type: ignore[arg-type]
            lease_token=row["lease_token"],
            lease_expires_at=(
                _from_iso(row["lease_expires_at"]) if row["lease_expires_at"] else None
            ),  # type: ignore[arg-type]
            next_retry_at=(
                _from_iso(row["next_retry_at"]) if row["next_retry_at"] else None
            ),  # type: ignore[arg-type]
            transcript_path=row["transcript_path"],
            error=error,
            acked_at=(
                _from_iso(row["acked_at"]) if row["acked_at"] else None  # type: ignore[arg-type]
            ),
        )


def _iso(dt: datetime | None) -> str | None:
    """Serialize datetime to ISO 8601 string, ensuring UTC tzinfo.

    Naive datetimes are assumed UTC. Always produces a value that
    round-trips through `datetime.fromisoformat()` preserving tzinfo.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _from_iso(s: str) -> datetime:
    """Parse ISO 8601 string. Accepts the trailing 'Z' suffix (3.11+)."""
    return datetime.fromisoformat(s)