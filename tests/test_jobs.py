"""End-to-end tests for the `/jobs/*` endpoints.

Wire-contract assertions follow HLD-001 §9.2 (202 + poll_url on
submit; transcript + transcript_path on the state response for
DONE jobs; YouTube-only URL validation per HLD-001 O-3).
"""

from __future__ import annotations

from httpx import AsyncClient

from local_transcription_service.config import Settings
from local_transcription_service.models import JobError, JobStatus
from local_transcription_service.queue.store import JobStore

YOUTUBE_URLS = [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=abc",
    "https://m.youtube.com/watch?v=abc",
    "https://youtu.be/dQw4w9WgXcQ",
]
NON_YOUTUBE_URLS = [
    "https://example.com/watch?v=abc",
    "https://vimeo.com/123456",
    "https://music.youtube.com/watch?v=abc",  # not in MVP allowlist
    "ftp://www.youtube.com/watch?v=abc",  # wrong scheme
]


# ---------- POST /jobs ----------


async def test_submit_job_accepted_with_poll_url(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/jobs",
        json={"video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        headers=auth_headers,
    )
    assert response.status_code == 202
    body = response.json()
    assert "job_id" in body
    assert body["status"] == "queued"
    assert body["poll_url"] == f"/jobs/{body['job_id']}"


async def test_submit_job_rejects_invalid_url_format(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/jobs",
        json={"video_url": "not-a-url"},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_submit_job_rejects_missing_url(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post("/jobs", json={}, headers=auth_headers)
    assert response.status_code == 422


async def test_submit_job_rejects_extra_fields(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/jobs",
        json={"video_url": "https://example.com", "extra": "field"},
        headers=auth_headers,
    )
    # ConfigDict(extra="forbid") on SubmitJobRequest
    assert response.status_code == 422


async def test_submit_job_accepts_all_youtube_hosts(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """HLD-001 O-3: YouTube URLs only for MVP."""
    for url in YOUTUBE_URLS:
        response = await client.post(
            "/jobs", json={"video_url": url}, headers=auth_headers
        )
        assert response.status_code == 202, f"rejected {url}: {response.text}"


async def test_submit_job_rejects_non_youtube_urls(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Non-YouTube hosts and non-http schemes are rejected with 422."""
    for url in NON_YOUTUBE_URLS:
        response = await client.post(
            "/jobs", json={"video_url": url}, headers=auth_headers
        )
        assert response.status_code == 422, f"accepted {url}: {response.text}"


# ---------- GET /jobs/{id} ----------


async def test_get_job_returns_state(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = await client.post(
        "/jobs",
        json={"video_url": "https://www.youtube.com/watch?v=abc"},
        headers=auth_headers,
    )
    job_id = created.json()["job_id"]
    response = await client.get(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job_id
    assert body["status"] == "queued"
    assert body["attempt"] == 0
    assert body["error"] is None
    assert body["transcript"] is None
    assert body["transcript_path"] is None
    assert "created_at" in body


async def test_get_unknown_job_returns_404(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/jobs/does-not-exist", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"


async def test_get_job_includes_error_when_failed(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: JobStore,
) -> None:
    created = await client.post(
        "/jobs",
        json={"video_url": "https://www.youtube.com/watch?v=err"},
        headers=auth_headers,
    )
    job_id = created.json()["job_id"]
    claimed = await store.claim(lease_ttl_seconds=60)
    assert claimed is not None
    await store.mark_processing(claimed.job_id, lease_token=claimed.lease_token)
    await store.mark_failed(
        claimed.job_id,
        lease_token=claimed.lease_token,
        error=JobError(code="E_TEST", message="bad", retryable=False),
    )
    response = await client.get(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == JobStatus.FAILED.value
    assert body["error"] == {
        "code": "E_TEST",
        "message": "bad",
        "retryable": False,
    }


async def test_get_job_includes_transcript_and_path_when_done(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: JobStore,
    settings: Settings,
) -> None:
    """HLD-001 §9.2: DONE state response must carry transcript + transcript_path."""
    created = await client.post(
        "/jobs",
        json={"video_url": "https://www.youtube.com/watch?v=done"},
        headers=auth_headers,
    )
    job_id = created.json()["job_id"]
    claimed = await store.claim(lease_ttl_seconds=60)
    assert claimed is not None
    await store.mark_processing(claimed.job_id, lease_token=claimed.lease_token)
    transcript_path = settings.results_dir / f"{job_id}.md"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text("hello world", encoding="utf-8")
    await store.mark_done(
        claimed.job_id,
        lease_token=claimed.lease_token,
        transcript_path=str(transcript_path),
    )

    response = await client.get(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == JobStatus.DONE.value
    assert body["transcript"] == "hello world"
    assert body["transcript_path"] == str(transcript_path)


# ---------- GET /jobs/{id}/result ----------


async def test_get_result_404_when_still_queued(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = await client.post(
        "/jobs",
        json={"video_url": "https://www.youtube.com/watch?v=pending"},
        headers=auth_headers,
    )
    job_id = created.json()["job_id"]
    response = await client.get(f"/jobs/{job_id}/result", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_READY"


async def test_get_result_404_for_unknown_job(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/jobs/missing/result", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"


async def test_get_result_returns_text_when_done(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: JobStore,
    settings: Settings,
) -> None:
    created = await client.post(
        "/jobs",
        json={"video_url": "https://www.youtube.com/watch?v=done"},
        headers=auth_headers,
    )
    job_id = created.json()["job_id"]
    claimed = await store.claim(lease_ttl_seconds=60)
    assert claimed is not None
    await store.mark_processing(claimed.job_id, lease_token=claimed.lease_token)
    transcript_path = settings.results_dir / f"{job_id}.md"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text("hello world", encoding="utf-8")
    await store.mark_done(
        claimed.job_id,
        lease_token=claimed.lease_token,
        transcript_path=str(transcript_path),
    )
    response = await client.get(f"/jobs/{job_id}/result", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "hello world"


async def test_get_result_410_when_failed(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: JobStore,
) -> None:
    created = await client.post(
        "/jobs",
        json={"video_url": "https://www.youtube.com/watch?v=fail"},
        headers=auth_headers,
    )
    job_id = created.json()["job_id"]
    claimed = await store.claim(lease_ttl_seconds=60)
    assert claimed is not None
    await store.mark_processing(claimed.job_id, lease_token=claimed.lease_token)
    await store.mark_failed(
        claimed.job_id,
        lease_token=claimed.lease_token,
        error=JobError(code="TEST", message="forced failure", retryable=False),
    )
    response = await client.get(f"/jobs/{job_id}/result", headers=auth_headers)
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "JOB_FAILED"


async def test_get_result_500_when_done_but_file_missing(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: JobStore,
    settings: Settings,
) -> None:
    """DONE in DB but transcript file gone from disk."""
    created = await client.post(
        "/jobs",
        json={"video_url": "https://www.youtube.com/watch?v=ghost"},
        headers=auth_headers,
    )
    job_id = created.json()["job_id"]
    claimed = await store.claim(lease_ttl_seconds=60)
    assert claimed is not None
    await store.mark_processing(claimed.job_id, lease_token=claimed.lease_token)
    ghost_path = settings.results_dir / f"{job_id}.md"
    await store.mark_done(
        claimed.job_id,
        lease_token=claimed.lease_token,
        transcript_path=str(ghost_path),
    )
    assert not ghost_path.exists()  # confirm setup

    response = await client.get(f"/jobs/{job_id}/result", headers=auth_headers)
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "TRANSCRIPT_MISSING"
