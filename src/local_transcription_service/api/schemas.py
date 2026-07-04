"""Pydantic request/response schemas for the public HTTP API.

These are intentionally separate from `models.py` (internal domain
types) so the wire contract stays decoupled from storage and
pipeline code. Adding a field to the internal `Job` dataclass
should NOT silently leak into the public API.

Field shapes follow HLD-001 §6 (POST/GET `/jobs` responses).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from local_transcription_service.models import JobError, JobStatus

# HLD-001 O-3: "YouTube URLs only for MVP. Direct file URL / upload
# is a future HLD." The set covers the canonical YouTube hostnames
# the extension copies links from. New formats (e.g. music.youtube.com)
# are added case-by-case — don't broaden the set without an HLD bump.
_YOUTUBE_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }
)


class SubmitJobRequest(BaseModel):
    """Request body for `POST /jobs`.

    `video_url` is validated as a syntactically valid HTTP URL and
    then narrowed to YouTube hosts only. Both checks happen at
    deserialization time, so any non-YouTube URL yields a 422
    response before reaching the route handler.
    """

    model_config = ConfigDict(extra="forbid")

    video_url: Annotated[
        HttpUrl,
        Field(description="YouTube video URL to transcribe (MVP restriction)"),
    ]

    @field_validator("video_url")
    @classmethod
    def _must_be_youtube(cls, value: HttpUrl) -> HttpUrl:
        host = (value.host or "").lower()
        if host not in _YOUTUBE_HOSTS:
            msg = f"video_url must be a YouTube URL; got host={host!r}"
            raise ValueError(msg)
        return value


class SubmitJobResponse(BaseModel):
    """Response body for `POST /jobs` (HTTP 202 Accepted, HLD-001 §6).

    `poll_url` is a server-relative path the client can poll to read
    job state. We deliberately do NOT return a full URL — the
    service is bound to a LAN IP and clients are expected to know
    the base.
    """

    job_id: str
    status: JobStatus
    poll_url: str


class JobStateResponse(BaseModel):
    """Response body for `GET /jobs/{id}` (HLD-001 §6).

    Field semantics:
    - `transcript`: full transcript text when `status == "done"`, else null.
    - `transcript_path`: file path on the server when done, else null.
    - `error`: structured `JobError` when `status == "failed"`, else null.
    - `acked_at`: ISO 8601 UTC timestamp of the first successful
      `POST /jobs/{id}/ack`, or `null` if the job has not yet been
      acked (or was never acked because it failed). Set on terminal
      `done` jobs once the extension confirms download. Lets the
      extension reconcile local state without a separate ack call.

    `error` is a dataclass; Pydantic v2 serializes it as a nested
    object (code/message/retryable) via `arbitrary_types_allowed`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    job_id: str
    video_url: str
    status: JobStatus
    attempt: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: JobError | None = None
    transcript: str | None = None
    transcript_path: str | None = None
    acked_at: datetime | None = None


class ErrorPayload(BaseModel):
    """Structured error body returned by all `4xx`/`5xx` responses.

    Field shape mirrors `JobError` so clients can use one parser
    for HTTP errors and failed-job payloads.
    """

    code: str
    message: str
    retryable: bool = False


class AckResponse(BaseModel):
    """Response body for `POST /jobs/{job_id}/ack` (HLD-001 §13.1).

    The endpoint is idempotent: a repeated ack of an already-acked
    job returns 200 with ``already_acked=True`` and an unchanged
    ``acked_at`` timestamp.

    Field semantics:

    - ``transcript_moved`` — observed FS state at return time:
      ``True`` iff the path the DB currently points at is on disk
      inside the trash directory. ``False`` if the file is missing
      (operator deletion between calls) or has not been moved yet.
      This is what the extension reads as "is the transcript
      actually downloadable from your system?".
    - ``transcript_path`` — the path the server currently believes
      the transcript lives at, after this call. After a successful
      first ack this is `${LTS_DATA_DIR}/trash/{filename}`. After a
      retry that auto-healed a stale DB path (file actually at
      trash, but a previous call's `update_transcript_path` failed),
      this is also the canonical trash path discovered by
      `move_to_trash`. May legitimately equal the old DB path with
      ``transcript_moved=False`` when the file is missing
      everywhere — the extension sees "the file is gone, the path
      I had isn't valid", which is actionable.

    The two fields together let the extension reconcile across
    retries without a separate filesystem probe.
    """

    job_id: str
    acked_at: datetime
    already_acked: bool
    transcript_moved: bool
    transcript_path: str | None = None
