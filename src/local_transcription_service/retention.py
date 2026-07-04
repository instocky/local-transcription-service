"""Trash retention automation (HLD-001 §13.2, Phase D, 2026-07-04).

The retention policy deletes files from ``${LTS_DATA_DIR}/trash/`` based
on two independent knobs (TTL + size cap). The CLI entry point reads
the knobs from env at start, runs the policy, and exits.

A two-knob policy was chosen over a single "size or age" cap because:

- TTL is what operators reason about ("transcripts older than a week").
- Size cap is the disk-budget safety net ("don't let trash exceed N").
- Both run in one pass; each is independent and idempotent.

The CLI is invoked by the launchd plist once a day at 04:00 local
(``com.local-transcription-service.trash-cleanup``). Operators can
also run it by hand: ``lts-trash-cleanup --dry-run`` to preview the
deletion set, or without flags for the real thing.

Module shape (TASK-D §3.2):

- :class:`TrashEntry` — input to the policy (one row of the dir walk).
- :class:`RetentionPolicy` — the two-knob policy + the pure
  ``select_for_deletion`` function. Pure means no I/O — tests can call
  it without a tmpdir.
- :class:`CleanupReport` — frozen dataclass with the counts.
- :func:`run_cleanup` — the I/O wrapper. Walks the dir, builds
  ``TrashEntry`` rows, asks the policy what to delete, unlinks or
  skips per ``dry_run``.
- :func:`main` / :func:`amain` — CLI entry points. ``main`` parses
  argv + env and dispatches to ``amain``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_DEFAULT_TTL_DAYS: int = 7
_DEFAULT_MAX_BYTES: int = 512 * 1024 * 1024  # 512 MiB
_SECONDS_PER_DAY: int = 86_400


@dataclass(frozen=True)
class TrashEntry:
    """One file the retention policy considers for deletion.

    Pure value type — no methods, no I/O. The caller (``run_cleanup``)
    builds a list of these from a real directory walk; tests build
    them by hand for the pure-function unit tests.

    ``mtime`` is a POSIX timestamp (seconds since epoch) — matches
    ``Path.stat().st_mtime`` so the caller does not have to convert.
    """

    path: Path
    mtime: float
    size: int


@dataclass(frozen=True)
class RetentionPolicy:
    """Two-knob retention policy (HLD-001 §13.2).

    Both knobs default-on; both are tunable via env vars on the CLI.
    The defaults match TASK-D §3.1: 7-day TTL, 512 MiB size cap.
    """

    ttl_days: int = _DEFAULT_TTL_DAYS
    max_bytes: int = _DEFAULT_MAX_BYTES

    def __post_init__(self) -> None:
        """Validate the policy values. Run by ``@dataclass(frozen=True)``."""
        if self.ttl_days < 0:
            msg = f"ttl_days must be >= 0, got {self.ttl_days}"
            raise ValueError(msg)
        if self.max_bytes < 0:
            msg = f"max_bytes must be >= 0, got {self.max_bytes}"
            raise ValueError(msg)

    def select_for_deletion(
        self,
        files: Iterable[TrashEntry],
        *,
        now: float,
    ) -> list[TrashEntry]:
        """Pure function. Returns the files to delete, oldest first.

        Two passes, in order:

        1. **TTL**: any file with ``mtime < now - ttl_days * 86400`` is
           selected. Surviving files proceed to the size pass.
        2. **Size cap**: of the survivors, if their cumulative size
           exceeds ``max_bytes``, the oldest (by ``mtime``) are
           selected until the survivors fit under the cap. "Fit under
           the cap" means *cumulative survivors size* ≤ ``max_bytes``
           AFTER the deletion — we delete enough oldest files to make
           room.

        Returns a list of :class:`TrashEntry` sorted oldest-first
        (matches the algorithm's deletion order, useful for
        deterministic dry-run output).

        This function does **no** I/O — it can be unit-tested with
        hand-built ``TrashEntry`` lists and no tmpdir. ``run_cleanup``
        is the only path that touches the filesystem.
        """
        # Materialize once and sort by mtime ascending — both passes
        # walk the same ordering (oldest first).
        all_files = sorted(files, key=lambda f: f.mtime)
        ttl_cutoff = now - self.ttl_days * _SECONDS_PER_DAY

        # Pass 1 — TTL.
        ttl_selected: list[TrashEntry] = []
        survivors: list[TrashEntry] = []
        for entry in all_files:
            if entry.mtime < ttl_cutoff:
                ttl_selected.append(entry)
            else:
                survivors.append(entry)

        if not survivors:
            return ttl_selected

        # Pass 2 — size cap. Operates on survivors only; their total
        # size is what we need to fit under max_bytes.
        survivors_sorted = sorted(survivors, key=lambda f: f.mtime)
        survivors_total = sum(f.size for f in survivors_sorted)

        if survivors_total <= self.max_bytes:
            return ttl_selected

        # Need to free `survivors_total - max_bytes` bytes. Delete
        # oldest until target is reached. Track freed cumulatively;
        # the last file we add may overshoot — that's fine, we want
        # to land ≤ max_bytes.
        size_selected: list[TrashEntry] = []
        freed = 0
        target = survivors_total - self.max_bytes
        for entry in survivors_sorted:
            if freed >= target:
                break
            size_selected.append(entry)
            freed += entry.size

        # Combine: TTL-selected + size-selected. The combined list is
        # already sorted oldest-first because both passes walked the
        # same ordering.
        return ttl_selected + size_selected


@dataclass(frozen=True)
class CleanupReport:
    """Result of one retention pass.

    ``deleted`` and ``kept`` count files (not bytes). ``freed_bytes``
    is the sum of ``size`` for the files actually unlinked — i.e. the
    bytes reclaimed by this run. ``dry_run`` mirrors the input flag so
    the report can be inspected after the fact.
    """

    deleted: int
    kept: int
    freed_bytes: int
    dry_run: bool


async def run_cleanup(
    *,
    trash_dir: Path,
    policy: RetentionPolicy,
    dry_run: bool = False,
    logger: logging.Logger | None = None,
    now: float | None = None,
) -> CleanupReport:
    """One retention pass.

    Walks ``trash_dir`` (non-recursive — flat dir, matches the
    service's ``move_to_trash`` contract), asks the policy what to
    delete, unlinks or skips per ``dry_run``, returns the counts.

    The function does not raise on per-file errors — a missing
    file (race with operator) is logged and skipped, the rest of
    the pass continues. Catastrophic errors (the dir itself is
    unreadable, permissions on the dir) propagate; the CLI catches
    them and exits 2.

    Args:
        trash_dir: directory to scan. Created if missing (matches
            the rest of the service's "ensure dirs" discipline).
        policy: the retention policy to apply.
        dry_run: if True, compute the deletion set but do NOT unlink.
        logger: optional logger for the run. Defaults to this module's
            logger.
        now: optional POSIX timestamp to evaluate the TTL against.
            Defaults to ``time.time()`` — tests pass an explicit value
            to keep the run deterministic.

    Returns:
        CleanupReport with the counts.
    """
    log = logger or logging.getLogger(__name__)
    current_now = now if now is not None else time.time()

    # Path operations are technically blocking I/O; we run them in
    # a thread to keep the event loop responsive. The walk is small
    # (one dir, flat) so this is mostly a correctness marker — a
    # large trash dir would otherwise stall the loop while stat()ing
    # thousands of files.
    if not await asyncio.to_thread(trash_dir.exists):
        log.info(
            "retention: trash_dir does not exist, creating",
            extra={"event": "retention_trash_dir_created", "path": str(trash_dir)},
        )
        await asyncio.to_thread(trash_dir.mkdir, parents=True, exist_ok=True)
        return CleanupReport(deleted=0, kept=0, freed_bytes=0, dry_run=dry_run)

    if not await asyncio.to_thread(trash_dir.is_dir):
        msg = f"trash_dir exists but is not a directory: {trash_dir}"
        raise NotADirectoryError(msg)

    # Walk the dir — flat, non-recursive. The service writes transcripts
    # as `${trash_dir}/{job_id}.md` and never nests; if an operator has
    # dropped a subdir in there by accident, we skip it (logged) and
    # leave it alone.
    entries: list[TrashEntry] = []
    skipped_subdirs: list[str] = []
    try:
        children = await asyncio.to_thread(list, trash_dir.iterdir())  # noqa: ASYNC240
        for child in children:
            try:
                # Skip subdirectories — defensive. They shouldn't exist
                # per the service contract; if they do, the operator
                # left them there and we leave them alone.
                if await asyncio.to_thread(child.is_dir):
                    skipped_subdirs.append(str(child))
                    continue
                # Symlinks: stat follows the link by default — the
                # size we get is the target's size, which is what the
                # policy wants to count. unlink() does NOT follow,
                # so the symlink itself is removed without touching
                # the target. See test_run_cleanup_symlink_does_not_follow.
                stat_result = await asyncio.to_thread(child.stat)
            except OSError as exc:
                log.warning(
                    "retention: stat failed, skipping",
                    extra={"event": "retention_stat_failed", "path": str(child), "error": str(exc)},
                )
                continue
            entries.append(
                TrashEntry(path=child, mtime=stat_result.st_mtime, size=stat_result.st_size),
            )
    except OSError as exc:
        # Catastrophic — the dir itself is unreadable. Surface to the
        # CLI, which maps to exit code 2.
        msg = f"failed to iterate trash_dir {trash_dir}: {exc}"
        raise OSError(msg) from exc

    if skipped_subdirs:
        log.warning(
            "retention: skipping subdirectories",
            extra={"event": "retention_subdirs_skipped", "paths": skipped_subdirs},
        )

    selection = policy.select_for_deletion(entries, now=current_now)
    selection_paths = {e.path for e in selection}

    deleted = 0
    freed_bytes = 0

    if dry_run:
        # Log what *would* be deleted — same JSON shape as the real
        # run's final line, but with deleted=0 and the planned
        # deletions in extra fields. Operators use this to preview.
        log.info(
            "retention: dry-run",
            extra={
                "event": "retention_dry_run",
                "would_delete": len(selection),
                "would_keep": len(entries) - len(selection),
                "paths": [str(e.path) for e in selection],
                "policy": {
                    "ttl_days": policy.ttl_days,
                    "max_bytes": policy.max_bytes,
                },
            },
        )
        return CleanupReport(
            deleted=0,
            kept=len(entries),
            freed_bytes=0,
            dry_run=True,
        )

    for entry in entries:
        if entry.path not in selection_paths:
            continue
        try:
            # missing_ok=True: handles the "operator deleted the file
            # between iterdir and unlink" race. The race only loses
            # one unlink; the count is still right.
            await asyncio.to_thread(entry.path.unlink, missing_ok=True)
            deleted += 1
            freed_bytes += entry.size
        except OSError as exc:
            log.warning(
                "retention: unlink failed",
                extra={
                    "event": "retention_unlink_failed",
                    "path": str(entry.path),
                    "error": str(exc),
                },
            )

    kept = len(entries) - deleted
    log.info(
        "retention: complete",
        extra={
            "event": "retention_complete",
            "deleted": deleted,
            "kept": kept,
            "freed_bytes": freed_bytes,
            "dry_run": False,
            "policy": {
                "ttl_days": policy.ttl_days,
                "max_bytes": policy.max_bytes,
            },
        },
    )
    return CleanupReport(deleted=deleted, kept=kept, freed_bytes=freed_bytes, dry_run=False)


# ---------- CLI ----------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser. Pure — no env reads."""
    parser = argparse.ArgumentParser(
        prog="lts-trash-cleanup",
        description=(
            "Trash retention cleanup (HLD-001 §13.2). Reads "
            "LTS_TRASH_TTL_DAYS and LTS_TRASH_MAX_BYTES from env; "
            "deletes files from ${LTS_DATA_DIR}/trash/ that exceed "
            "the policy. Idempotent — running twice in a row is a "
            "no-op the second time."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the deletion set without unlinking. Exits 0.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Override LTS_DATA_DIR for one-off runs against a non-default "
            "data directory. Default: ${LTS_DATA_DIR} or ~/.local-transcription."
        ),
    )
    return parser


def _resolve_data_dir(args_data_dir: Path | None) -> Path:
    """Resolve the data dir from CLI arg or env or default.

    Precedence: --data-dir flag > LTS_DATA_DIR env > ~/.local-transcription.
    """
    if args_data_dir is not None:
        return args_data_dir.expanduser().resolve()
    env_value = os.getenv("LTS_DATA_DIR")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return Path.home() / ".local-transcription"


def _resolve_policy() -> RetentionPolicy:
    """Resolve the policy from LTS_TRASH_* env vars.

    Invalid values raise ValueError — the CLI catches and exits 1.
    """
    raw_ttl = os.getenv("LTS_TRASH_TTL_DAYS")
    raw_max = os.getenv("LTS_TRASH_MAX_BYTES")
    ttl_days = int(raw_ttl) if raw_ttl is not None else _DEFAULT_TTL_DAYS
    max_bytes = int(raw_max) if raw_max is not None else _DEFAULT_MAX_BYTES
    return RetentionPolicy(ttl_days=ttl_days, max_bytes=max_bytes)


def _configure_cli_logging() -> None:
    """Configure root logger for the standalone CLI.

    The service uses a JSON formatter; for the CLI we still emit JSON
    so an operator piping into ``jq`` gets the same shape as the
    in-process service logs. If the root logger is already configured
    (e.g., a parent process set it up), we leave it alone.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    handler = logging.StreamHandler(stream=sys.stderr)

    class _JsonFormatter(logging.Formatter):
        """Minimal JSON formatter for the CLI's stderr feed."""

        def format(self, record: logging.LogRecord) -> str:
            payload: dict[str, object] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            # Merge `extra={...}` fields from the log call.
            reserved = {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            }
            for key, value in record.__dict__.items():
                if key in reserved or key.startswith("_"):
                    continue
                payload[key] = value
            if record.exc_info:
                payload["exc_info"] = self.formatException(record.exc_info)
            return json.dumps(payload, default=str)

    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


async def amain(argv: list[str] | None = None) -> int:
    """Async CLI entry. Returns the process exit code."""
    _configure_cli_logging()
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        data_dir = _resolve_data_dir(args.data_dir)
    except (OSError, ValueError) as exc:
        logger.error(
            "retention: failed to resolve data_dir",
            extra={"event": "retention_config_error", "error": str(exc)},
        )
        return 1

    try:
        policy = _resolve_policy()
    except ValueError as exc:
        logger.error(
            "retention: invalid policy env",
            extra={"event": "retention_config_error", "error": str(exc)},
        )
        return 1

    trash_dir = data_dir / "trash"

    try:
        report = await run_cleanup(
            trash_dir=trash_dir,
            policy=policy,
            dry_run=args.dry_run,
        )
    except (OSError, NotADirectoryError) as exc:
        logger.error(
            "retention: I/O error",
            extra={"event": "retention_io_error", "error": str(exc)},
        )
        return 2

    # Emit the human-readable summary on stdout so operators running
    # the CLI by hand see the counts. The structured log line above
    # already has the same numbers for log-feed consumers.
    sys.stdout.write(
        f"retention: deleted={report.deleted} kept={report.kept} "
        f"freed_bytes={report.freed_bytes} dry_run={report.dry_run}\n"
    )
    sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Sync entry point for the ``lts-trash-cleanup`` console-script."""
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())