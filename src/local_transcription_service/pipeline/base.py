"""Pipeline abstraction and the mock implementation for Phase A.

A pipeline takes a video URL plus its `job_id` (so temp files in
`audio-cache/` can be scoped per job) and returns the transcript
text. The worker writes the returned text to
`settings.results_dir` and records the path via `mark_done`. This
split keeps the pipeline focused on STT and lets the worker own
disk layout + lifecycle.

The real STT engine (whisper.cpp behind LiteLLM, default; mock for
dev/CI) is wired in Phase B by composing the three stages defined
in `pipeline/stages.py` (Stage 1 yt-dlp, Stage 2 ffmpeg, Stage 3
delegated to an injected `STTEngine`).

Pipelines signal a failure by raising. Use `PipelineError` to
communicate the retry semantics (see HLD-001 §10 + §12); any other
exception is treated as retryable by the worker (defensive
default — we assume the failure is transient unless the pipeline
explicitly says otherwise).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class PipelineError(Exception):
    """Pipeline failure with explicit retry semantics.

    `code` becomes the structured `JobError.code` on the job.
    `retryable=True` lets the worker reschedule the job after the
    configured backoff (HLD-001 §10). `retryable=False` is a hard
    fail — the job goes to FAILED immediately.

    Canonical codes are pinned in HLD-001 §12 (the HLD is the
    source of truth). The codes this module emits directly:

    - ``FETCH_FAILED`` — Stage 1 (yt-dlp) failure.
      ``retryable=False`` for binary missing / non-zero exit;
      ``retryable=True`` for network errors (so a transient drop
      is retried after the §10 backoff).
    - ``AUDIO_CONDITIONING_FAILED`` — Stage 2 (ffmpeg) failure.
      Always ``retryable=False``; ffmpeg problems are operator-fix
      (install binary, fix disk).
    - ``MODEL_NOT_PULLED`` — STT engine failure (emitted by Stage 3
      implementations). ``retryable=False``; the model has to be
      registered in LiteLLM before resubmitting.

    Any other ``Exception`` raised by a pipeline is treated by the
    worker as a transient retryable failure; that fallback lives
    in ``worker._error_from_exception``, not here.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "FETCH_FAILED",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TranscriptionPipeline(ABC):
    """Abstract STT pipeline.

    Implementations are responsible for download, decode, and model
    inference (HLD-001 §6). They MUST NOT touch the job store or
    write transcript files — the worker owns those concerns.

    `job_id` is threaded through so the pipeline can scope its
    intermediate files under `${LTS_DATA_DIR}/audio-cache/{job_id}.*`
    and clean them up deterministically on success and failure
    (HLD-001 §11). The pipeline does not own the id (the worker /
    job store does); it just uses it as a stable key on disk.
    """

    @abstractmethod
    async def transcribe(self, video_url: str, *, job_id: str) -> str:
        """Run the pipeline and return the transcript text.

        `job_id` is a stable per-job identifier used to scope
        intermediate files in `audio-cache/`. It is keyword-only
        so the call site reads cleanly and can't be silently
        reordered against a future positional argument.

        The returned string is written verbatim to the transcript
        file as UTF-8. Raise `PipelineError` to communicate retry
        semantics; the worker converts it into a `JobError` and
        either defers the job or marks it FAILED.
        """


class MockPipeline(TranscriptionPipeline):
    """Deterministic pipeline for development and tests.

    No I/O, no model. Returns a single line of text derived from
    the input URL. Replace with `WhisperPipeline` (real three-stage
    pipeline, defined in `pipeline/stages.py`) in production.
    """

    async def transcribe(self, video_url: str, *, job_id: str) -> str:
        return f"mock transcript for {video_url} (job_id={job_id})\n"


__all__ = ["MockPipeline", "PipelineError", "TranscriptionPipeline"]