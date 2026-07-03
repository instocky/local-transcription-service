"""The `STTEngine` protocol — Stage 3 contract (HLD-001 §4).

This is the *engine* half of the two-layer transcribe contract
(TASK-B §2). It owns only speech-to-text inference for a single
already-conditioned WAV file. The orchestration layer
(`TranscriptionPipeline`) owns Stage 1→2→3 sequencing and temp-file
layout; the worker owns the job store and `results/`.

Engines signal failure by raising `pipeline.base.PipelineError` with
the correct retry semantics so the worker's existing retry policy
(HLD-001 §10) applies without special-casing STT.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class STTEngine(Protocol):
    """Stage 3 speech-to-text engine.

    Implementations transcribe a 16 kHz mono PCM WAV (produced by
    Stage 2) into plain text. They MUST NOT read or write the job
    store, the results directory, or the audio cache — those belong
    to the worker and the orchestrating pipeline respectively.
    """

    async def transcribe(self, wav_path: Path, *, language: str | None = None) -> str:
        """Transcribe `wav_path` and return the plain-text transcript.

        `language` is an optional ISO-639-1 hint passed to the engine;
        `None` lets the engine auto-detect. Raise `PipelineError` to
        communicate retry semantics to the worker.
        """
        ...

    async def is_ready(self) -> bool:
        """Report whether the engine can serve a transcription now.

        Used by the `/ready` probe (HLD-001 §8). Must not raise —
        an unreachable backend reports `False`, not an exception.
        """
        ...
