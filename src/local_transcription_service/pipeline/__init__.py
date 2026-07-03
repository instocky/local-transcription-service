"""STT pipeline abstraction.

Defines the ``TranscriptionPipeline`` interface, the
``MockPipeline`` used during Phase A, and the real three-stage
``WhisperPipeline`` (yt-dlp → ffmpeg → STTEngine) added in Phase B.
``WhisperPipeline`` is the canonical public name per TASK-B §B4 /
HLD-001 §6; ``RealPipeline`` (the same class) is kept as an alias
so pre-B4 tests that imported the implementation name keep working.
"""

from local_transcription_service.pipeline.base import (
    MockPipeline,
    PipelineError,
    TranscriptionPipeline,
)
from local_transcription_service.pipeline.stages import (
    RealPipeline,
    STTEngine,
    cleanup_job_files,
    condition_audio,
    fetch_media,
)
from local_transcription_service.pipeline.whisper_pipeline import WhisperPipeline

__all__ = [
    "MockPipeline",
    "PipelineError",
    "RealPipeline",
    "STTEngine",
    "TranscriptionPipeline",
    "WhisperPipeline",
    "cleanup_job_files",
    "condition_audio",
    "fetch_media",
]