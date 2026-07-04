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


class ErrorPayload(BaseModel):
    """Structured error body returned by all `4xx`/`5xx` responses.

    Field shape mirrors `JobError` so clients can use one parser
    for HTTP errors and failed-job payloads.
    """

    code: str
    message: str
    retryable: bool = False
