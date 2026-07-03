"""FastAPI application entry point.

`create_app(settings, store, pipeline)` builds the app from
explicitly provided dependencies — no module-level state. The
production `main()` entry point constructs those dependencies
and starts uvicorn + the worker in the same event loop.
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
from local_transcription_service.pipeline.base import MockPipeline
from local_transcription_service.queue.store import JobStore
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


def main() -> None:
    """Console entry point for `local-transcription-service`.

    Starts the HTTP server and the background worker in the same
    event loop. Uvicorn handles SIGINT/SIGTERM; on graceful exit
    the worker is stopped and awaited.
    """
    settings = get_settings()
    settings.ensure_dirs()

    async def _run() -> None:
        store = JobStore(settings.db_path)
        await store.init()
        pipeline: TranscriptionPipeline = MockPipeline()
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
