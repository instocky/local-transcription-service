"""Pipeline abstraction and the mock implementation for Phase A.

A pipeline takes a video URL and returns the transcript text. The
worker writes the returned text to `settings.results_dir` and
records the path via `mark_done`. This split keeps the pipeline
focused on STT and lets the worker own disk layout + lifecycle.

The real STT engines (ollama HTTP, mlx-whisper) are added in
Phase B. The mock returns a deterministic string derived from
the video URL so tests and golden transcripts are reproducible.

Pipelines signal a failure by raising. Use `PipelineError` to
communicate the retry semantics (see HLD-001 §10); any other
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

    Common codes:
    - "PIPELINE_TRANSIENT" — network blip, ollama restart, etc.
    - "INVALID_URL" — non-YouTube / private / region-locked video.
    - "MODEL_MISSING" — STT model not pulled (ollama) or weights absent.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "PIPELINE_TRANSIENT",
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
    """

    @abstractmethod
    async def transcribe(self, video_url: str) -> str:
        """Run the pipeline and return the transcript text.

        The returned string is written verbatim to the transcript
        file as UTF-8. Raise `PipelineError` to communicate retry
        semantics; the worker converts it into a `JobError` and
        either defers the job or marks it FAILED.
        """


class MockPipeline(TranscriptionPipeline):
    """Deterministic pipeline for development and tests.

    No I/O, no model. Returns a single line of text derived from
    the input URL. Replace with `OllamaPipeline` in Phase B.
    """

    async def transcribe(self, video_url: str) -> str:
        return f"mock transcript for {video_url}\n"

