"""Smoke test for the /health endpoint."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from local_transcription_service.app import create_app
from local_transcription_service.config import Settings
from local_transcription_service.pipeline.base import MockPipeline
from local_transcription_service.queue.store import JobStore

AUTH_TOKEN = "test-token-aaaaaaaaaa"


async def test_health_returns_ok_and_version(tmp_path) -> None:
    settings = Settings(auth_token=AUTH_TOKEN, data_dir=tmp_path)
    store = JobStore(settings.db_path)
    await store.init()
    app = create_app(settings=settings, store=store, pipeline=MockPipeline())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "version" in payload
