"""STT pipeline abstraction.

Defines the ``TranscriptionPipeline`` interface, the
``MockPipeline`` used during Phase A, and the real three-stage
``RealPipeline`` (yt-dlp → ffmpeg → STTEngine) added in Phase B.
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

__all__ = [
    "MockPipeline",
    "PipelineError",
    "RealPipeline",
    "STTEngine",
    "TranscriptionPipeline",
    "cleanup_job_files",
    "condition_audio",
    "fetch_media",
]