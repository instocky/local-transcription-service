"""The canonical ``WhisperPipeline`` orchestrator (TASK-B §B4 / HLD-001 §11).

``WhisperPipeline`` is the public name for the real three-stage
transcription pipeline:

1. **fetch**    — ``yt-dlp`` downloads the source media into
   ``${audio_cache_dir}/{job_id}.{ext}``.
2. **condition** — ``ffmpeg`` normalises to 16 kHz mono PCM WAV at
   ``${audio_cache_dir}/{job_id}.wav``.
3. **stt**      — delegated to the injected STT engine
   (see :class:`~local_transcription_service.stt.base.STTEngine`).

Cleanup of every ``{audio_cache_dir}/{job_id}.*`` file is guaranteed
on success and failure via a ``try/finally`` block (HLD-001 §11).

This module is the *canonical* home of the orchestrator class per
TASK-B §B4; the underlying stage implementations live in
:mod:`local_transcription_service.pipeline.stages`. The class is
exposed there as ``RealPipeline`` (the implementation name that
landed in B1) and re-exported here as ``WhisperPipeline`` — they
are the same class, so ``isinstance(p, RealPipeline)`` and
``isinstance(p, WhisperPipeline)`` are both true, and pre-B4 tests
that imported ``RealPipeline`` keep working.

Structured stage logging (HLD-001 §15) is emitted here, around each
stage call:

.. code-block:: json

    {"event":"stage_started","stage":"fetch","job_id":"..."}
    {"event":"stage_finished","stage":"fetch","job_id":"...","duration_s":4.2}
    {"event":"stage_started","stage":"condition","job_id":"..."}
    ...
    {"event":"stage_started","stage":"stt","job_id":"..."}
    {"event":"stage_finished","stage":"stt","job_id":"...","duration_s":2.1}

The stage names (``fetch`` / ``condition`` / ``stt``) match the HLD
field values exactly, so log aggregators can group / alert on the
three phases without per-implementation dispatch.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from local_transcription_service.pipeline.stages import (
    RealPipeline,
    cleanup_job_files,
    condition_audio,
    fetch_media,
)

logger = logging.getLogger(__name__)


# Stage names — emitted verbatim into the ``stage`` field of the
# stage_started / stage_finished JSON events per HLD-001 §15.
_STAGE_FETCH = "fetch"
_STAGE_CONDITION = "condition"
_STAGE_STT = "stt"


def _log_stage_started(stage: str, job_id: str) -> None:
    logger.info(
        "stage started",
        extra={"event": "stage_started", "stage": stage, "job_id": job_id},
    )


def _log_stage_finished(stage: str, job_id: str, duration_s: float) -> None:
    logger.info(
        "stage finished",
        extra={
            "event": "stage_finished",
            "stage": stage,
            "job_id": job_id,
            "duration_s": round(duration_s, 3),
        },
    )


class WhisperPipeline(RealPipeline):
    """Three-stage STT pipeline: ``yt-dlp`` → ``ffmpeg`` → ``STTEngine``.

    Concrete orchestrator named per TASK-B §B4 / HLD-001 §6. Same
    behaviour as :class:`~local_transcription_service.pipeline.stages.RealPipeline`
    (which is the same class — ``WhisperPipeline = RealPipeline``
    via inheritance) plus structured stage logging on every call.

    Construction signature is identical to ``RealPipeline``: an
    :class:`STTEngine` instance, an audio cache directory, and the
    optional ``ytdlp_bin`` / ``ffmpeg_bin`` overrides used by tests
    to point at fake binaries.
    """

    async def transcribe(self, video_url: str, *, job_id: str) -> str:
        """Run Stage 1 → Stage 2 → Stage 3 and clean up on every path.

        Each stage is bracketed by a ``stage_started`` /
        ``stage_finished`` JSON event (HLD-001 §15) carrying the
        job id, the stage name (``fetch`` / ``condition`` / ``stt``)
        and the elapsed wall-clock duration. On any failure path the
        cleanup helper still runs and removes every
        ``{audio_cache_dir}/{job_id}.*`` file before the exception
        propagates.
        """
        cache_dir: Path = self._audio_cache_dir  # type: ignore[attr-defined]
        wav_path = cache_dir / f"{job_id}.wav"
        try:
            # Stage 1 — fetch
            _log_stage_started(_STAGE_FETCH, job_id)
            t0 = time.perf_counter()
            raw_path = await fetch_media(
                cache_dir,
                video_url,
                job_id,
                ytdlp_bin=self._ytdlp_bin,  # type: ignore[attr-defined]
            )
            _log_stage_finished(_STAGE_FETCH, job_id, time.perf_counter() - t0)

            # Stage 2 — condition
            _log_stage_started(_STAGE_CONDITION, job_id)
            t0 = time.perf_counter()
            await condition_audio(
                raw_path,
                wav_path,
                ffmpeg_bin=self._ffmpeg_bin,  # type: ignore[attr-defined]
            )
            _log_stage_finished(_STAGE_CONDITION, job_id, time.perf_counter() - t0)

            # Stage 3 — STT (delegated to the injected engine)
            _log_stage_started(_STAGE_STT, job_id)
            t0 = time.perf_counter()
            text = await self._stt_engine.transcribe(wav_path)  # type: ignore[attr-defined]
            _log_stage_finished(_STAGE_STT, job_id, time.perf_counter() - t0)

            return text
        finally:
            cleanup_job_files(cache_dir, job_id)


__all__ = ["WhisperPipeline"]