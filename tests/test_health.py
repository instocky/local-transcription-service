"""Smoke test for the /health endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from local_transcription_service.app import create_app


def test_health_returns_ok_and_version() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "version" in payload