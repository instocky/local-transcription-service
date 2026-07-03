"""Deterministic in-process STT engine for CI and dev (HLD-001 §4).

`MockSTT` is selected via `LTS_STT_ENGINE=mock`. It performs no I/O
and needs no gateway, so the Phase A test suite and CI stay green
without a whisper.cpp deployment on the box.
"""

from __future__ import annotations

from pathlib import Path


class MockSTT:
    """Return a deterministic transcript derived from the WAV filename.

    Stage 2 writes the conditioned WAV as `{job_id}.wav` (HLD-001 §11),
    so `wav_path.stem` is the job id — the mock echoes it, mirroring
    `MockPipeline`'s deterministic output so golden transcripts stay
    reproducible.
    """

    async def transcribe(self, wav_path: Path, *, language: str | None = None) -> str:
        return f"mock transcript for {wav_path.stem}\n"

    async def is_ready(self) -> bool:
        return True
