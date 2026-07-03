"""Stage 3 speech-to-text engines (HLD-001 §4).

The `STTEngine` protocol decouples the orchestration pipeline from the
concrete STT backend. Two implementations ship in Phase B:

- `LiteLLMWhisperSTT` — POSTs the WAV to the whisper.cpp deployment
  behind the LiteLLM gateway (`LTS_STT_ENGINE=openai`, the default).
- `MockSTT` — deterministic, no I/O; used in CI / dev
  (`LTS_STT_ENGINE=mock`).

Engines raise `PipelineError` (from `pipeline.base`) so the worker's
existing retry semantics apply unchanged — the STT layer never touches
the job store or the results directory.
"""

from __future__ import annotations

from local_transcription_service.stt.base import STTEngine
from local_transcription_service.stt.litellm_whisper import LiteLLMWhisperSTT
from local_transcription_service.stt.mock import MockSTT

__all__ = ["LiteLLMWhisperSTT", "MockSTT", "STTEngine"]
