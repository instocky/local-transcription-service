"""FastAPI application entry point.

`create_app(settings, store, pipeline)` builds the app from
explicitly provided dependencies — no module-level state. The
production `main()` entry point constructs the ``STTEngine``
selected by ``settings.stt_engine``, wraps it in a
``WhisperPipeline``, configures JSON logging (HLD-001 §15), and
starts uvicorn + the worker in the same event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI

from local_transcription_service import __version__
from local_transcription_service.api import health, jobs
from local_transcription_service.config import Settings, get_settings
from local_transcription_service.logging import configure_logging
from local_transcription_service.pipeline.whisper_pipeline import WhisperPipeline
from local_transcription_service.queue.store import JobStore
from local_transcription_service.stt.base import STTEngine
from local_transcription_service.stt.litellm_whisper import LiteLLMWhisperSTT
from local_transcription_service.stt.mock import MockSTT
from local_transcription_service.worker import Worker

if TYPE_CHECKING:
    from local_transcription_service.pipeline.base import TranscriptionPipeline

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings,
    store: JobStore,
    pipeline: TranscriptionPipeline | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    The caller owns `settings`, `store`, and `pipeline` — typically
    `main()` in production, a test fixture in development. The app
    stashes the dependencies on `app.state` for endpoint handlers
    to read via `request.app.state.*`.

    DI shape is unchanged from Phase A: ``create_app(settings, store, pipeline)``.
    The new wiring happens inside ``main()`` — this function does
    not pick engines from settings, so tests can still pass an
    arbitrary ``MockPipeline`` (or no pipeline at all) without
    needing a real STT gateway.
    """
    app = FastAPI(
        title="Local Transcription Service",
        version=__version__,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.pipeline = pipeline

    app.include_router(health.router)
    app.include_router(jobs.router)
    return app


def build_stt_engine(settings: Settings) -> STTEngine:
    """Instantiate the Stage-3 ``STTEngine`` from settings (TASK-B §B4).

    Mapping (HLD-001 §4 amended):

    - ``stt_engine == "openai"`` → :class:`LiteLLMWhisperSTT` talking
      to the LiteLLM Proxy at ``settings.stt_base_url`` with
      ``settings.stt_api_key`` as the bearer token and
      ``settings.stt_model`` as the model id.
    - ``stt_engine == "mock"``   → :class:`MockSTT` (no I/O, used by
      CI / dev when no gateway is reachable).

    ``Settings._check_openai_requires_api_key`` already guards
    ``stt_engine == "openai"`` against an empty api key, so by the
    time we get here the config is valid.

    Kept as a module-level function so tests can exercise the
    dispatch without going through the full ``main()`` setup.
    """
    if settings.stt_engine == "mock":
        return MockSTT()
    if settings.stt_engine == "openai":
        return LiteLLMWhisperSTT(
            base_url=settings.stt_base_url,
            api_key=settings.stt_api_key,
            model=settings.stt_model,
        )
    msg = f"unsupported stt_engine: {settings.stt_engine!r}"
    raise ValueError(msg)


def build_pipeline(settings: Settings, engine: STTEngine) -> TranscriptionPipeline:
    """Construct the orchestration pipeline for production wiring.

    Production builds a :class:`WhisperPipeline` (real three-stage
    yt-dlp → ffmpeg → STTEngine orchestrator, TASK-B §B4). Tests
    can substitute :class:`MockPipeline` directly via ``create_app``.
    """
    return WhisperPipeline(stt_engine=engine, audio_cache_dir=settings.audio_cache_dir)


def _log_config_resolved(settings: Settings) -> None:
    """Emit the HLD-001 §15 ``config_resolved`` startup event.

    Fields mirror the HLD example: ``stt_engine``, ``stt_model``,
    ``bind_host``, ``bind_port``, ``data_dir``, ``lease_ttl_s``,
    ``max_attempts``. The auth token is **never** logged — it is a
    secret and would also be a footgun in any aggregated log feed.
    """
    logger.info(
        "config resolved",
        extra={
            "event": "config_resolved",
            "stt_engine": settings.stt_engine,
            "stt_model": settings.stt_model,
            "bind_host": settings.bind_host,
            "bind_port": settings.bind_port,
            "data_dir": str(settings.data_dir),
            "lease_ttl_s": settings.lease_ttl_seconds,
            "max_attempts": settings.max_attempts,
        },
    )


def main() -> None:
    """Console entry point for `local-transcription-service`.

    Wires the production STT engine + pipeline, configures JSON
    logging, then starts the HTTP server and the background worker
    in the same event loop. Uvicorn handles SIGINT/SIGTERM; on
    graceful exit the worker is stopped and awaited.
    """
    settings = get_settings()
    settings.ensure_dirs()

    configure_logging(level="INFO")
    _log_config_resolved(settings)

    engine = build_stt_engine(settings)
    pipeline = build_pipeline(settings, engine)

    async def _run() -> None:
        store = JobStore(settings.db_path)
        await store.init()
        app = create_app(settings=settings, store=store, pipeline=pipeline)
        worker = Worker(store, pipeline, settings)

        config = uvicorn.Config(
            app,
            host=settings.bind_host,
            port=settings.bind_port,
            log_level="info",
        )
        server = uvicorn.Server(config)

        worker_task = asyncio.create_task(worker.run_forever(), name="lts-worker")
        try:
            await server.serve()
        finally:
            worker.stop()
            await worker_task

    asyncio.run(_run())


if __name__ == "__main__":
    main()