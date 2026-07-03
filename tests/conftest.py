"""Shared pytest fixtures.

Every test gets a fresh on-disk data directory (`tmp_path`),
an initialized `JobStore`, a FastAPI app wired against that
store, and an `httpx.AsyncClient` for hitting endpoints without
the Starlette `TestClient` deprecation warning.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from local_transcription_service.app import create_app
from local_transcription_service.config import Settings
from local_transcription_service.pipeline.base import MockPipeline
from local_transcription_service.queue.store import JobStore

AUTH_TOKEN = "test-token-aaaaaaaaaa"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a per-test data directory."""
    return Settings(
        auth_token=AUTH_TOKEN,
        data_dir=tmp_path,
        lease_ttl_seconds=600,
        reclaim_interval_seconds=60,
        max_attempts=2,
    )


@pytest_asyncio.fixture
async def store(settings: Settings) -> JobStore:
    """Initialized JobStore bound to the test settings' DB path."""
    s = JobStore(settings.db_path)
    await s.init()
    return s


@pytest_asyncio.fixture
async def app(
    settings: Settings,
    store: JobStore,
) -> FastAPI:
    """Fresh FastAPI app wired to the test settings and store."""
    pipeline = MockPipeline()
    return create_app(settings=settings, store=store, pipeline=pipeline)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """AsyncClient bound to the test app via ASGI transport (no real socket)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Headers with the correct `X-Auth-Token`."""
    return {"X-Auth-Token": AUTH_TOKEN}
