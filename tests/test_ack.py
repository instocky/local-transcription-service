"""End-to-end tests for `POST /jobs/{job_id}/ack` (HLD-001 §13.1).

Drives the job lifecycle via the public API + the underlying store,
then asserts on:

- 200 newly acked (DB row gets `acked_at`, file ends up in `trash/`).
- 200 idempotent retry (second call is a no-op, `already_acked=True`).
- 404 unknown job, 409 not-done (`queued`/`claimed`/`processing`/`failed`).
- 401 missing/bad token, mirroring the existing auth contract.
- Subsequent `GET /jobs/{id}` and `GET /jobs/{id}/result` still work
  after ack (file moved, DB path updated).
- FS-move failure: `acked_at` written, but `transcript_moved=False`
  in the response — DB is the source of truth, FS is best-effort.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from httpx import AsyncClient

from local_transcription_service.config import Settings
from local_transcription_service.models import JobError, JobStatus
from local_transcription_service.queue.store import JobStore

# ---------- helpers ----------


async def _submit_then_complete(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
    *,
    url: str = "https://www.youtube.com/watch?v=ack",
) -> tuple[str, str]:
    """POST a job, claim→process→done, write the transcript file.

    Returns ``(job_id, transcript_path)``.
    """
    created = await client.post(
        "/jobs",
        json={"video_url": url},
        headers=auth_headers,
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]

    claimed = await store.claim(lease_ttl_seconds=60)
    assert claimed is not None
    assert claimed.job_id == job_id
    token = claimed.lease_token or ""
    await store.mark_processing(claimed.job_id, lease_token=token)

    transcript_path = settings.results_dir / f"{job_id}.md"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text("hello transcript", encoding="utf-8")
    assert await store.mark_done(
        claimed.job_id,
        lease_token=token,
        transcript_path=str(transcript_path),
    )
    return job_id, str(transcript_path)


# ---------- happy path ----------


async def test_ack_returns_200_and_sets_acked_at(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    job_id, _ = await _submit_then_complete(client, store, settings, auth_headers)

    response = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == job_id
    assert body["already_acked"] is False
    assert body["transcript_moved"] is True
    assert "acked_at" in body
    # transcript_path in the response must be the new (trash) location.
    assert body["transcript_path"] is not None
    assert "trash" in body["transcript_path"]
    assert body["transcript_path"].endswith(".md")

    # DB persisted.
    refreshed = await store.get(job_id)
    assert refreshed is not None
    assert refreshed.acked_at is not None


async def test_ack_actually_moves_file_to_trash(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    job_id, transcript_path_str = await _submit_then_complete(
        client, store, settings, auth_headers
    )
    transcript_path = Path(transcript_path_str)
    assert transcript_path.exists()  # noqa: ASYNC240 - sync FS in test setup
    assert not (settings.trash_dir / f"{job_id}.md").exists()  # noqa: ASYNC240 - sync FS in test setup

    response = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert response.status_code == 200

    assert not transcript_path.exists(), "source must be gone after move"  # noqa: ASYNC240
    moved_to = settings.trash_dir / f"{job_id}.md"
    assert moved_to.exists(), "destination must exist"  # noqa: ASYNC240
    assert moved_to.read_text(encoding="utf-8") == "hello transcript"


async def test_ack_updates_transcript_path_in_db(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    """After ack, the DB row's transcript_path must point to the
    new location so subsequent GET /jobs/{id}/result still streams."""
    job_id, _ = await _submit_then_complete(client, store, settings, auth_headers)

    response = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert response.status_code == 200

    refreshed = await store.get(job_id)
    assert refreshed is not None
    assert refreshed.transcript_path is not None
    assert refreshed.transcript_path.startswith(str(settings.trash_dir))


# ---------- idempotency ----------


async def test_ack_is_idempotent(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    job_id, _ = await _submit_then_complete(client, store, settings, auth_headers)

    first = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert first.status_code == 200
    assert first.json()["already_acked"] is False
    first_acked_at = first.json()["acked_at"]

    second = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert second.status_code == 200
    assert second.json()["already_acked"] is True
    # Idempotent: original acked_at is preserved, not "now" again.
    assert second.json()["acked_at"] == first_acked_at
    # Second call sees the file already in trash, reports moved=True.
    assert second.json()["transcript_moved"] is True


# ---------- 404 / 409 / 401 ----------


async def test_ack_returns_404_for_unknown_job(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post("/jobs/no-such-job/ack", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"


async def test_ack_returns_409_for_queued_job(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = await client.post(
        "/jobs",
        json={"video_url": "https://www.youtube.com/watch?v=q"},
        headers=auth_headers,
    )
    job_id = created.json()["job_id"]
    response = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["code"] == "NOT_DONE"
    assert "queued" in body["detail"]["message"]


async def test_ack_returns_409_for_failed_job(
    client: AsyncClient,
    store: JobStore,
    auth_headers: dict[str, str],
) -> None:
    created = await client.post(
        "/jobs",
        json={"video_url": "https://www.youtube.com/watch?v=fail-ack"},
        headers=auth_headers,
    )
    job_id = created.json()["job_id"]
    claimed = await store.claim(lease_ttl_seconds=60)
    assert claimed is not None
    token = claimed.lease_token or ""
    await store.mark_processing(claimed.job_id, lease_token=token)
    await store.mark_failed(
        claimed.job_id,
        lease_token=token,
        error=JobError(code="E", message="e", retryable=False),
    )
    response = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "NOT_DONE"


async def test_ack_requires_auth(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    job_id, _ = await _submit_then_complete(client, store, settings, auth_headers)

    # No header.
    no_auth = await client.post(f"/jobs/{job_id}/ack")
    assert no_auth.status_code == 401

    # Wrong header.
    wrong = await client.post(
        f"/jobs/{job_id}/ack", headers={"X-Auth-Token": "definitely-wrong"}
    )
    assert wrong.status_code == 401


# ---------- downstream integration: post-ack reads still work ----------


async def test_get_job_after_ack_includes_acked_at_and_new_path(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    """After ack, GET /jobs/{id} must surface acked_at + the trash path.

    The test is the contract pin for the new ``acked_at`` field on
    ``JobStateResponse``; without this assertion a regression that
    drops the field would not be caught (Phase C review, 2026-07-04).
    """
    job_id, _ = await _submit_then_complete(client, store, settings, auth_headers)

    # Pre-ack: acked_at must be absent (null) so we know the post-ack
    # assertion below is testing the change, not just persistence.
    pre = await client.get(f"/jobs/{job_id}", headers=auth_headers)
    assert pre.status_code == 200
    assert pre.json()["acked_at"] is None

    ack_response = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert ack_response.status_code == 200
    expected_acked_at = ack_response.json()["acked_at"]
    assert expected_acked_at is not None  # sanity — what we compare against

    response = await client.get(f"/jobs/{job_id}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == JobStatus.DONE.value
    assert body["transcript"] == "hello transcript"
    # transcript_path follows the move.
    assert body["transcript_path"] is not None
    assert body["transcript_path"].startswith(str(settings.trash_dir))
    # The new contract: acked_at is exposed via GET /jobs/{id} with
    # the same value the ack response returned (idempotent — preserved
    # across retries, not bumped to "now").
    assert body["acked_at"] == expected_acked_at


async def test_get_result_after_ack_streams_from_trash(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    """HLD-001 §13.1: GET /jobs/{id}/result must keep working after ack —
    the DB row's transcript_path has been updated to the trash location."""
    job_id, _ = await _submit_then_complete(client, store, settings, auth_headers)
    await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)

    response = await client.get(f"/jobs/{job_id}/result", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "hello transcript"


# ---------- FS-move failure: best-effort ----------


async def test_ack_succeeds_when_fs_move_fails(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    """DB is the source of truth; FS move is best-effort. If the move
    raises an OSError (e.g. permission denied), the response is still
    200 with `transcript_moved=False` and the job stays acked.
    """
    job_id, _ = await _submit_then_complete(client, store, settings, auth_headers)

    # Patch Path.replace to raise an OSError on this call only.
    with patch(
        "local_transcription_service.queue.transcripts.Path.replace",
        side_effect=OSError("simulated permission denied"),
    ):
        response = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["already_acked"] is False
    assert body["transcript_moved"] is False
    # DB row IS acked — the contract says the move is best-effort.
    refreshed = await store.get(job_id)
    assert refreshed is not None
    assert refreshed.acked_at is not None
    # And a retry without the FS failure moves it cleanly.
    retry = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert retry.status_code == 200
    assert retry.json()["already_acked"] is True
    assert retry.json()["transcript_moved"] is True


async def test_ack_reports_transcript_moved_false_when_file_missing_from_trash(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    """If the DB path already points into trash_dir but the operator
    deleted the file manually, a retry must surface the actual
    filesystem state (``transcript_moved=False``), not the stale
    DB-resident path. Path-only checks would lie here (P1 finding
    from Phase C review, 2026-07-04).
    """
    job_id, _ = await _submit_then_complete(client, store, settings, auth_headers)
    # First ack: succeeds, file ends up in trash/.
    first = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert first.status_code == 200
    assert first.json()["transcript_moved"] is True

    # Simulate operator cleanup: delete the file from trash/.
    moved_path = settings.trash_dir / f"{job_id}.md"
    assert moved_path.exists()  # confirm setup
    moved_path.unlink()

    # Retry: DB still says `acked_at` set + transcript_path in trash,
    # but the FS check must catch the missing file.
    retry = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert retry.status_code == 200
    body = retry.json()
    assert body["already_acked"] is True
    # Critical: must be False even though the path still points into trash/.
    assert body["transcript_moved"] is False


async def test_ack_returns_503_on_database_failure(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    """A database failure during `mark_acked` is mapped to
    ``503 Service Unavailable`` (P3 finding — previously the unhandled
    aiosqlite.Error fell through to FastAPI's default 500).
    """
    import aiosqlite

    job_id, _ = await _submit_then_complete(client, store, settings, auth_headers)

    with patch.object(
        store,
        "mark_acked",
        side_effect=aiosqlite.Error("simulated disk full"),
    ):
        response = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DB_UNAVAILABLE"


async def test_ack_converges_after_update_transcript_path_failure(
    client: AsyncClient,
    store: JobStore,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    """P1 regression test (Phase C review 2026-07-04).

    If the first ack successfully moves the file to trash but the
    follow-up DB write fails, the next ack must auto-heal the
    stale DB path. Without auto-discovery in `move_to_trash`, the
    retry would see "source missing" → "destination=None" → DB
    stays stale → `GET /jobs/{id}/result` keeps 500ing.
    """
    import aiosqlite

    job_id, _ = await _submit_then_complete(client, store, settings, auth_headers)

    # First ack: simulate the partial-failure window.
    with patch.object(
        store,
        "update_transcript_path",
        side_effect=aiosqlite.Error("simulated write failure"),
    ):
        response = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert response.status_code == 503

    # Confirm the partial state: file is at trash, DB has stale path.
    assert (settings.trash_dir / f"{job_id}.md").exists()  # noqa: ASYNC240
    refreshed = await store.get(job_id)
    assert refreshed is not None
    assert refreshed.transcript_path is not None
    assert refreshed.transcript_path.startswith(str(settings.results_dir))

    # Retry without the patch — auto-discovery should heal the path.
    retry = await client.post(f"/jobs/{job_id}/ack", headers=auth_headers)
    assert retry.status_code == 200
    body = retry.json()
    assert body["already_acked"] is True
    assert body["transcript_moved"] is True
    assert body["transcript_path"] == str(settings.trash_dir / f"{job_id}.md")

    # DB now reflects the trash path.
    refreshed = await store.get(job_id)
    assert refreshed is not None
    assert refreshed.transcript_path == str(settings.trash_dir / f"{job_id}.md")

    # And GET /jobs/{id}/result must work again.
    stream = await client.get(f"/jobs/{job_id}/result", headers=auth_headers)
    assert stream.status_code == 200
    assert stream.text == "hello transcript"
