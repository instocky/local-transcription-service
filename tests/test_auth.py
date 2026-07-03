"""Tests for `X-Auth-Token` enforcement."""

from __future__ import annotations

from httpx import AsyncClient


async def test_jobs_rejects_missing_token(client: AsyncClient) -> None:
    response = await client.get("/jobs/anything")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Token"
    detail = response.json()["detail"]
    assert detail["code"] == "UNAUTHORIZED"


async def test_jobs_rejects_wrong_token(client: AsyncClient) -> None:
    response = await client.get(
        "/jobs/anything",
        headers={"X-Auth-Token": "definitely-not-the-right-token"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "UNAUTHORIZED"


async def test_jobs_accepts_correct_token(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    # Token passes the auth dependency; the route then returns 404
    # because the job doesn't exist. Either way, NOT 401.
    response = await client.get("/jobs/nonexistent", headers=auth_headers)
    assert response.status_code == 404


async def test_post_jobs_rejects_missing_token(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/jobs",
        json={"video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert response.status_code == 401


async def test_health_does_not_require_auth(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200


async def test_ready_does_not_require_auth(client: AsyncClient) -> None:
    # /ready probes local binaries; status may be 200 or 503, but
    # never 401.
    response = await client.get("/ready")
    assert response.status_code in (200, 503)
