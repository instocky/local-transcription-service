"""Transcript file lifecycle (HLD-001 §13.1, Phase C).

The transcript file moves through three places on disk:

1.  ``${LTS_DATA_DIR}/audio-cache/{job_id}.{ext}`` — raw media download
    (Stage 1, cleaned up before STT is even called).
2.  ``${LTS_DATA_DIR}/results/{job_id}.md}`` — finished transcript
    (Stage 3 writes here via ``mark_done``).
3.  ``${LTS_DATA_DIR}/trash/{job_id}.md}`` — after the extension
    acks the download (this module's responsibility).

The move in step 3 is a single ``Path.replace`` call — atomic on the
same volume on both POSIX and Windows. If the source is missing
because the operator (or a previous failed ack) already cleaned it
up, the move is treated as a success: the FS is the secondary
record; the DB ``transcript_path`` is what subsequent reads follow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MoveOutcome:
    """Result of attempting to move a transcript into the trash dir.

    The HTTP layer maps ``destination`` to ``transcript_path`` in the
    response: a non-``None`` value tells the extension where to find
    the file if it needs to retry the download, even when the move
    itself was a no-op.
    """

    moved: bool          # True iff this call performed the rename.
    destination: Path | None  # Path the file lives at after the call.


def move_to_trash(
    *,
    job_id: str,
    source: Path | None,
    trash_dir: Path,
) -> MoveOutcome:
    """Atomically move a finished transcript into ``trash_dir``.

    Args:
        job_id: Used only for logging — the destination filename is
            derived from the source filename, not ``job_id`` directly,
            so an operator who relocated the file earlier keeps that
            basename.
        source: Current transcript path (``None`` if ``transcript_path``
            in the DB was already empty — rare, only on partial writes
            from an older code path).
        trash_dir: Destination directory. Created if missing (idempotent).

    Returns:
        ``MoveOutcome`` with ``moved=True`` if the file was actually
        renamed by this call (or was already in ``trash_dir`` with
        the expected basename — operator-cleaned leftovers count as
        already-moved for idempotency purposes). ``destination`` is
        the path the file lives at after the call, regardless of
        whether we performed the rename.

    Failure modes:

    - ``OSError`` other than ``FileNotFoundError``: logged, ``moved=False``,
      ``destination=None``. Caller surfaces this in the response
      (``transcript_moved=False``) but still acknowledges the job —
      the DB is the source of truth.
    - Source doesn't exist: logged, ``moved=False``, ``destination=None``.
      This can happen if an operator manually cleaned ``results/``
      or if the source filename doesn't match the canonical
      ``{job_id}.md`` pattern (e.g. older jobs from before the §11
      filename convention was enforced).
    """
    trash_dir.mkdir(parents=True, exist_ok=True)

    if source is None:
        logger.info(
            "transcript move: no source path recorded for job_id=%s; "
            "skipping FS move",
            job_id,
        )
        return MoveOutcome(moved=False, destination=None)

    if not source.exists():
        # Auto-discovery: the source is gone (already moved on a
        # previous call whose `update_transcript_path` failed, race
        # against operator cleanup, etc.). If the canonical filename
        # is sitting in trash_dir, return that as the destination so
        # the caller can heal the DB-stale path. Without this, the
        # endpoint cannot converge after a partial failure (P1
        # finding, Phase C review 2026-07-04).
        canonical = trash_dir / source.name
        if canonical.exists() and canonical.resolve() != source.resolve():
            logger.info(
                "transcript move: source missing but canonical found in trash "
                "for job_id=%s (canonical=%s) — auto-healing destination",
                job_id,
                canonical,
            )
            return MoveOutcome(moved=False, destination=canonical)
        logger.warning(
            "transcript move: source missing for job_id=%s at %s; "
            "leaving transcript_path unchanged",
            job_id,
            source,
        )
        return MoveOutcome(moved=False, destination=None)

    destination = trash_dir / source.name

    # If the file is already in the trash dir at the expected path,
    # nothing to do — but we still report destination so the caller
    # can sync the DB.
    try:
        if source.resolve() == destination.resolve():
            logger.debug(
                "transcript move: already in trash for job_id=%s at %s",
                job_id,
                destination,
            )
            return MoveOutcome(moved=False, destination=destination)
    except (OSError, ValueError):  # noqa: PERF203 - resolve can raise on Windows
        # If resolve fails (bad symlink, race), just attempt the move.
        pass

    try:
        source.replace(destination)
    except FileNotFoundError:
        # Lost a race with operator cleanup or another process.
        logger.warning(
            "transcript move: source disappeared mid-call for job_id=%s "
            "(source=%s)",
            job_id,
            source,
        )
        return MoveOutcome(moved=False, destination=None)
    except OSError as exc:
        logger.warning(
            "transcript move: rename failed for job_id=%s "
            "(source=%s, destination=%s, error=%s)",
            job_id,
            source,
            destination,
            exc,
        )
        return MoveOutcome(moved=False, destination=None)
    else:
        logger.info(
            "transcript moved to trash for job_id=%s (destination=%s)",
            job_id,
            destination,
        )
        return MoveOutcome(moved=True, destination=destination)
