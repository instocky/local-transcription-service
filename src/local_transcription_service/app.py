"""FastAPI application entry point.

This module currently exposes only `/health`. The job API and
processing pipeline are added once HLD-001 is approved.
"""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from local_transcription_service import __version__

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(
        title="Local Transcription Service",
        version=__version__,
        description=(
            "Companion service for the YT Transcript Copier Chrome extension. "
            "Performs local speech-to-text inference on a Mac Mini compute node."
        ),
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe. Returns service version and OK status."""
        return {"status": "ok", "version": __version__}

    return app


app = create_app()


def main() -> None:
    """Console entry point for `local-transcription-service`."""
    # Default bind is loopback only. Override via env when running
    # inside a container or for LAN access — see config.py.
    uvicorn.run(
        "local_transcription_service.app:app",
        host="127.0.0.1",
        port=8766,
    )


if __name__ == "__main__":
    main()