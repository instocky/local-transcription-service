"""Tests for the transcript-file lifecycle helpers (queue/transcripts.py).

The endpoint test in `test_ack.py` exercises the FS-move end-to-end
through the API; this file covers the helper's edge cases in isolation
so a regression is localised.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from local_transcription_service.queue.transcripts import move_to_trash


def test_move_to_trash_happy_path(tmp_path: Path) -> None:
    source = tmp_path / "results" / "job-1.md"
    source.parent.mkdir(parents=True)
    source.write_text("transcript body", encoding="utf-8")

    trash = tmp_path / "trash"
    outcome = move_to_trash(
        job_id="job-1",
        source=source,
        trash_dir=trash,
    )

    assert outcome.moved is True
    assert outcome.destination == trash / "job-1.md"
    assert not source.exists()
    assert (trash / "job-1.md").read_text(encoding="utf-8") == "transcript body"


def test_move_to_trash_when_source_missing(tmp_path: Path) -> None:
    """Source already gone: not an error, but we cannot claim a moved file."""
    source = tmp_path / "results" / "ghost.md"
    # Note: source is NOT created.
    trash = tmp_path / "trash"

    outcome = move_to_trash(
        job_id="ghost",
        source=source,
        trash_dir=trash,
    )
    assert outcome.moved is False
    assert outcome.destination is None
    # trash dir is created on first call so later writes succeed.
    assert trash.exists()


def test_move_to_trash_creates_trash_dir(tmp_path: Path) -> None:
    """Trash dir creation is idempotent and safe even if it doesn't pre-exist."""
    source = tmp_path / "results" / "job.md"
    source.parent.mkdir(parents=True)
    source.write_text("x", encoding="utf-8")

    trash = tmp_path / "deeper" / "trash" / "sub"
    assert not trash.exists()  # confirm setup

    outcome = move_to_trash(job_id="job", source=source, trash_dir=trash)
    assert outcome.moved is True
    assert trash.exists()


def test_move_to_trash_when_source_is_none(tmp_path: Path) -> None:
    """Defensive: transcript_path was None in the DB — no file to move."""
    outcome = move_to_trash(
        job_id="orphan",
        source=None,
        trash_dir=tmp_path / "trash",
    )
    assert outcome.moved is False
    assert outcome.destination is None


def test_move_to_trash_when_already_in_trash_dir(tmp_path: Path) -> None:
    """Source is already inside the trash dir: nothing to do, but report
    `destination` so the DB path stays accurate."""
    trash = tmp_path / "trash"
    trash.mkdir()
    file_in_trash = trash / "job.md"
    file_in_trash.write_text("x", encoding="utf-8")

    outcome = move_to_trash(
        job_id="job",
        source=file_in_trash,
        trash_dir=trash,
    )
    # Idempotent: nothing was renamed, but the destination is known.
    assert outcome.moved is False
    assert outcome.destination == file_in_trash
    assert file_in_trash.exists()


def test_move_to_trash_swallows_oserror(tmp_path: Path) -> None:
    """Path.replace raising OSError is logged + reported as no-move, not raised."""
    source = tmp_path / "job.md"
    source.write_text("x", encoding="utf-8")

    with patch(
        "local_transcription_service.queue.transcripts.Path.replace",
        side_effect=PermissionError("denied"),
    ):
        outcome = move_to_trash(
            job_id="job",
            source=source,
            trash_dir=tmp_path / "trash",
        )

    assert outcome.moved is False
    assert outcome.destination is None
    # Source is left where it was — operator can intervene.
    assert source.exists()


def test_move_to_trash_auto_discovers_when_source_missing(tmp_path: Path) -> None:
    """P1 fix (Phase C review, 2026-07-04): if the source is gone
    (already moved on a previous call whose `update_transcript_path`
    failed), `move_to_trash` looks for the canonical trash file and
    reports it as the destination so the caller can heal a stale
    DB path. Without this, the endpoint cannot converge after a
    partial-failure partial-state.
    """
    trash = tmp_path / "trash"
    trash.mkdir()
    canonical = trash / "renamed.md"
    canonical.write_text("previous-attempt body", encoding="utf-8")

    # Source matches by basename but doesn't exist on disk.
    ghost = tmp_path / "results" / "renamed.md"
    # Note: ghost is NOT created.

    outcome = move_to_trash(
        job_id="renamed",
        source=ghost,
        trash_dir=trash,
    )
    assert outcome.moved is False
    assert outcome.destination == canonical  # auto-discovered
    # No rename was performed — just the discovery.
    assert canonical.exists()
