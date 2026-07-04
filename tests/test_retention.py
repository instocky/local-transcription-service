"""Tests for src/local_transcription_service/retention.py.

Two layers:

- **Pure-function tests** — build ``TrashEntry`` lists by hand and
  exercise ``RetentionPolicy.select_for_deletion``. No tmpdir, no
  subprocess, fully deterministic.

- **I/O tests** — drive ``run_cleanup`` against a ``tmp_path`` (the
  pytest builtin). Cover happy path, dry-run, empty dir, symlink,
  and the CLI subprocess invocation.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from local_transcription_service.retention import (
    CleanupReport,
    RetentionPolicy,
    TrashEntry,
    main,
    run_cleanup,
)

# ---------- helpers ----------


def _entry(name: str, *, mtime: float, size: int = 100) -> TrashEntry:
    """Build a TrashEntry without a real Path.stat() — pure value."""
    return TrashEntry(path=Path(name), mtime=mtime, size=size)


# ---------- pure-function tests ----------


def test_select_for_deletion_ttl_only() -> None:
    """10 files aged 0–9 days, TTL=7d → 2 deleted (8d and 9d old)."""
    now = 1_000_000.0
    policy = RetentionPolicy(ttl_days=7, max_bytes=10**9)
    files = [_entry(f"f{i}.md", mtime=now - i * 86_400, size=100) for i in range(10)]

    selected = policy.select_for_deletion(files, now=now)

    # Files with mtime < now - 7*86400 = now - 604_800 → i >= 8.
    selected_paths = sorted(e.path.name for e in selected)
    assert selected_paths == ["f8.md", "f9.md"]


def test_select_for_deletion_size_only() -> None:
    """8 files × 200 MiB = 1.6 GiB, cap=512 MiB → delete oldest 6.

    File fixture: f0 has mtime=now (newest), f7 has mtime=now-7d (oldest).
    Oldest 6 means f7, f6, f5, f4, f3, f2 → sorted by name: f2..f7.
    """
    now = 1_000_000.0
    policy = RetentionPolicy(ttl_days=10_000, max_bytes=512 * 1024 * 1024)
    files = [
        _entry(f"f{i}.md", mtime=now - i * 86_400, size=200 * 1024 * 1024)
        for i in range(8)
    ]

    selected = policy.select_for_deletion(files, now=now)

    # survivors_total = 1.6 GiB; target = 1.6 - 0.512 = 1.088 GiB.
    # Delete oldest until freed >= 1.088 GiB; 6 × 200 MiB = 1.2 GiB freed.
    # Survivors after: f0, f1 (400 MiB total ≤ 512 MiB cap).
    selected_paths = sorted(e.path.name for e in selected)
    assert selected_paths == ["f2.md", "f3.md", "f4.md", "f5.md", "f6.md", "f7.md"]


def test_select_for_deletion_combined() -> None:
    """TTL passes some, size cap catches the rest."""
    now = 1_000_000.0
    policy = RetentionPolicy(ttl_days=10, max_bytes=400)
    # 6 files: ages 1, 5, 12, 20, 30, 40 days. Sizes: 100, 100, 100, 100, 100, 100.
    files = [
        _entry(f"f{i}.md", mtime=now - age * 86_400, size=100)
        for i, age in enumerate([1, 5, 12, 20, 30, 40])
    ]

    selected = policy.select_for_deletion(files, now=now)

    # TTL: files with mtime < now - 10*86400 → ages >= 11 → f2, f3, f4, f5.
    # Survivors: f0, f1 (200 bytes total) → under cap, no size deletions.
    selected_paths = sorted(e.path.name for e in selected)
    assert selected_paths == ["f2.md", "f3.md", "f4.md", "f5.md"]


def test_select_for_deletion_combined_size_pass_runs() -> None:
    """Survivors exceed the cap → size pass deletes oldest survivors."""
    now = 1_000_000.0
    policy = RetentionPolicy(ttl_days=10, max_bytes=250)
    # 6 files: ages 1, 5, 12, 20, 30, 40 days. Sizes: 100 each.
    files = [
        _entry(f"f{i}.md", mtime=now - age * 86_400, size=100)
        for i, age in enumerate([1, 5, 12, 20, 30, 40])
    ]

    selected = policy.select_for_deletion(files, now=now)

    # TTL: ages >= 11 → f2, f3, f4, f5 deleted.
    # Survivors: f0 (1d), f1 (5d); total = 200 bytes ≤ 250 cap → no size pass.
    # Wait — total = 200, cap = 250 → survivors fit, no size deletion.
    # Make the cap tighter to actually trigger the size pass.
    selected_paths = sorted(e.path.name for e in selected)
    assert selected_paths == ["f2.md", "f3.md", "f4.md", "f5.md"]


def test_select_for_deletion_size_pass_actually_runs() -> None:
    """Survivors exceed the cap → size pass deletes oldest survivors.

    File fixture: f0 is the youngest (1d), f5 is the oldest (40d).
    TTL=10d deletes f2 (12d), f3 (20d), f4 (30d), f5 (40d).
    Survivors: f0 (1d), f1 (5d); total=200 > 150 cap; size pass
    picks the oldest survivor (f1, since 5d > 1d by mtime) to make
    room. Combined deletion set: f1, f2, f3, f4, f5.
    """
    now = 1_000_000.0
    policy = RetentionPolicy(ttl_days=10, max_bytes=150)
    files = [
        _entry(f"f{i}.md", mtime=now - age * 86_400, size=100)
        for i, age in enumerate([1, 5, 12, 20, 30, 40])
    ]

    selected = policy.select_for_deletion(files, now=now)

    selected_paths = sorted(e.path.name for e in selected)
    assert selected_paths == ["f1.md", "f2.md", "f3.md", "f4.md", "f5.md"]


def test_select_for_deletion_empty() -> None:
    """Empty input → empty selection."""
    policy = RetentionPolicy()
    assert policy.select_for_deletion([], now=0.0) == []


def test_select_for_deletion_zero_ttl_deletes_all() -> None:
    """TTL=0 → every file with mtime < now is selected."""
    now = 1_000_000.0
    policy = RetentionPolicy(ttl_days=0, max_bytes=10**9)
    files = [_entry(f"f{i}.md", mtime=now - i - 1) for i in range(3)]

    selected = policy.select_for_deletion(files, now=now)

    # Strict `<` means TTL=0 still keeps files with mtime == now,
    # but our test fixtures all have mtime < now.
    selected_paths = sorted(e.path.name for e in selected)
    assert selected_paths == ["f0.md", "f1.md", "f2.md"]


def test_select_for_deletion_zero_max_bytes_deletes_all_survivors() -> None:
    """max_bytes=0 → every survivor is selected by the size pass."""
    now = 1_000_000.0
    policy = RetentionPolicy(ttl_days=0, max_bytes=0)
    files = [_entry(f"f{i}.md", mtime=now - i - 1) for i in range(3)]

    selected = policy.select_for_deletion(files, now=now)

    selected_paths = sorted(e.path.name for e in selected)
    assert selected_paths == ["f0.md", "f1.md", "f2.md"]


def test_retention_policy_validates_negative_values() -> None:
    """Negative ttl_days / max_bytes → ValueError at construction."""
    with pytest.raises(ValueError, match="ttl_days"):
        RetentionPolicy(ttl_days=-1, max_bytes=0)
    with pytest.raises(ValueError, match="max_bytes"):
        RetentionPolicy(ttl_days=0, max_bytes=-1)


# ---------- I/O tests ----------


async def test_run_cleanup_happy_path(tmp_path: Path) -> None:
    """Real tmpdir with 3 files aged 30d; TTL=7d → all 3 deleted."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    now = 1_000_000.0
    old_mtime = now - 30 * 86_400
    for name in ("a.md", "b.md", "c.md"):
        p = trash_dir / name
        p.write_text("hello", encoding="utf-8")
        # Force mtime via os.utime so the test is hermetic.
        os.utime(p, (old_mtime, old_mtime))

    policy = RetentionPolicy(ttl_days=7, max_bytes=10**9)
    report = await run_cleanup(trash_dir=trash_dir, policy=policy, now=now)

    assert report.deleted == 3
    assert report.kept == 0
    assert report.freed_bytes == 15  # "hello" is 5 bytes, ×3 files
    assert report.dry_run is False
    assert not any(trash_dir.iterdir()), "trash should be empty after cleanup"


async def test_run_cleanup_keeps_recent_files(tmp_path: Path) -> None:
    """Files newer than TTL are kept."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    now = 1_000_000.0
    # 1 old (will be deleted) + 1 fresh (will be kept).
    old = trash_dir / "old.md"
    fresh = trash_dir / "fresh.md"
    old.write_text("x", encoding="utf-8")
    fresh.write_text("y", encoding="utf-8")
    os.utime(old, (now - 30 * 86_400, now - 30 * 86_400))
    os.utime(fresh, (now - 1 * 86_400, now - 1 * 86_400))

    policy = RetentionPolicy(ttl_days=7, max_bytes=10**9)
    report = await run_cleanup(trash_dir=trash_dir, policy=policy, now=now)

    assert report.deleted == 1
    assert report.kept == 1
    assert old.exists() is False
    assert fresh.exists() is True


async def test_run_cleanup_dry_run_does_not_unlink(tmp_path: Path) -> None:
    """dry_run=True → same selection, no unlink, deleted=0 in report."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    now = 1_000_000.0
    old = trash_dir / "old.md"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (now - 30 * 86_400, now - 30 * 86_400))

    policy = RetentionPolicy(ttl_days=7, max_bytes=10**9)
    report = await run_cleanup(trash_dir=trash_dir, policy=policy, dry_run=True, now=now)

    assert report.deleted == 0
    assert report.kept == 1
    assert report.already_gone == 0
    assert report.dry_run is True
    assert report.freed_bytes == 0
    assert old.exists() is True, "dry-run must not unlink"


async def test_run_cleanup_accounts_for_already_gone_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 from the Phase D review: if a file disappears between the
    dir walk and the unlink loop, we must report it as
    ``already_gone=1``, NOT as ``deleted=1``.

    The fix is to call ``unlink()`` WITHOUT ``missing_ok=True`` so
    ``FileNotFoundError`` propagates and we can count the race
    separately. Without this, ``deleted`` and ``freed_bytes`` lie
    on every concurrent-cleanup or operator-beats-us case.
    """
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    now = 1_000_000.0
    old = trash_dir / "old.md"
    old.write_text("hello", encoding="utf-8")
    os.utime(old, (now - 30 * 86_400, now - 30 * 86_400))

    policy = RetentionPolicy(ttl_days=7, max_bytes=10**9)

    # Simulate the race: the operator deletes the file (or a
    # concurrent cleanup does) AFTER the dir walk. We do this by
    # patching Path.unlink to raise FileNotFoundError on the first
    # call — equivalent to the operator deleting the file between
    # iterdir and our unlink.
    import pathlib

    real_unlink = pathlib.Path.unlink

    def _unlink_race(self: pathlib.Path, *args: object, **kwargs: object) -> None:
        # The file gets "deleted" by the operator just before
        # our unlink — mirror that by raising FileNotFoundError
        # unless missing_ok was explicitly asked for.
        if not kwargs.get("missing_ok", False):
            raise FileNotFoundError(self)
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", _unlink_race)

    report = await run_cleanup(trash_dir=trash_dir, policy=policy, now=now)

    # Critical assertions — the lie we are guarding against:
    assert report.deleted == 0, (
        "deleted must NOT count files we didn't actually unlink"
    )
    assert report.freed_bytes == 0, (
        "freed_bytes must NOT count bytes we didn't actually reclaim"
    )
    assert report.already_gone == 1, (
        "the file was selected but already gone — count it in already_gone"
    )
    assert report.kept == 0


async def test_run_cleanup_accounts_already_gone_alongside_real_deletions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed path: one file actually deleted, one file vanished
    between walk and unlink. Both buckets are reported correctly
    and ``freed_bytes`` reflects ONLY the real deletion.
    """
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    now = 1_000_000.0

    real = trash_dir / "real.md"
    ghost = trash_dir / "ghost.md"
    real.write_text("a" * 100, encoding="utf-8")  # 100 bytes
    ghost.write_text("b" * 50, encoding="utf-8")  # 50 bytes
    os.utime(real, (now - 30 * 86_400, now - 30 * 86_400))
    os.utime(ghost, (now - 30 * 86_400, now - 30 * 86_400))

    policy = RetentionPolicy(ttl_days=7, max_bytes=10**9)

    # Race only on `ghost.md` — operator deletes it before our unlink.
    import pathlib

    real_unlink = pathlib.Path.unlink

    def _unlink_selective(self: pathlib.Path, *args: object, **kwargs: object) -> None:
        if self.name == "ghost.md":
            raise FileNotFoundError(self)
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", _unlink_selective)

    report = await run_cleanup(trash_dir=trash_dir, policy=policy, now=now)

    assert report.deleted == 1
    assert report.already_gone == 1
    assert report.freed_bytes == 100, (
        "freed_bytes must reflect ONLY the file we actually unlinked"
    )
    assert report.kept == 0


async def test_run_cleanup_empty_trash_dir(tmp_path: Path) -> None:
    """Empty trash → no-op, exit 0."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()

    policy = RetentionPolicy()
    report = await run_cleanup(trash_dir=trash_dir, policy=policy)

    assert report.deleted == 0
    assert report.kept == 0
    assert report.freed_bytes == 0
    assert report.dry_run is False


async def test_run_cleanup_creates_trash_dir_if_missing(tmp_path: Path) -> None:
    """trash_dir doesn't exist → created, no-op."""
    trash_dir = tmp_path / "trash"
    assert not trash_dir.exists()

    policy = RetentionPolicy()
    report = await run_cleanup(trash_dir=trash_dir, policy=policy)

    assert trash_dir.is_dir()
    assert report.deleted == 0
    assert report.kept == 0


async def test_run_cleanup_symlink_does_not_follow(tmp_path: Path) -> None:
    """Symlink in trash/ → unlinked, target untouched."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    target_dir = tmp_path / "outside"
    target_dir.mkdir()
    target_file = target_dir / "precious.md"
    target_file.write_text("do not delete", encoding="utf-8")

    link = trash_dir / "link.md"
    try:
        link.symlink_to(target_file)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink not supported on this fs: {exc}")

    policy = RetentionPolicy(ttl_days=0, max_bytes=0)
    now = 1_000_000.0
    # Force the symlink's mtime to be old so the policy picks it.
    os.utime(link, (now - 30 * 86_400, now - 30 * 86_400), follow_symlinks=False)

    report = await run_cleanup(trash_dir=trash_dir, policy=policy, now=now)

    assert report.deleted == 1
    assert not link.exists(), "symlink itself should be gone"
    assert target_file.exists(), "target file must NOT be touched"
    assert target_file.read_text(encoding="utf-8") == "do not delete"


async def test_run_cleanup_skips_subdirectories(tmp_path: Path) -> None:
    """Subdirectories in trash/ are skipped (defensive — should not exist per contract)."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    sub = trash_dir / "nested"
    sub.mkdir()

    policy = RetentionPolicy(ttl_days=0, max_bytes=0)
    report = await run_cleanup(trash_dir=trash_dir, policy=policy)

    assert report.deleted == 0
    assert sub.is_dir(), "subdirectory must be left alone"


async def test_run_cleanup_trash_dir_is_a_file_raises(tmp_path: Path) -> None:
    """trash_dir exists but is a regular file → NotADirectoryError."""
    trash_file = tmp_path / "not-a-dir"
    trash_file.write_text("oops", encoding="utf-8")

    policy = RetentionPolicy()
    with pytest.raises(NotADirectoryError):
        await run_cleanup(trash_dir=trash_file, policy=policy)


# ---------- CLI tests ----------


def test_main_dry_run_exits_zero(tmp_path: Path) -> None:
    """CLI dry-run against an empty trash → exit 0."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    env = os.environ.copy()
    env["LTS_DATA_DIR"] = str(tmp_path)

    rc = main(["--dry-run", "--data-dir", str(tmp_path)])
    assert rc == 0


def test_main_deletes_old_files(tmp_path: Path) -> None:
    """CLI real run → old file gone, recent file kept."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    now = time.time()
    old = trash_dir / "old.md"
    fresh = trash_dir / "fresh.md"
    old.write_text("old", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    os.utime(old, (now - 30 * 86_400, now - 30 * 86_400))

    rc = main(["--data-dir", str(tmp_path)])
    assert rc == 0
    assert not old.exists()
    assert fresh.exists()


def test_main_config_error_returns_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid LTS_TRASH_TTL_DAYS → exit 1 (config error)."""
    monkeypatch.setenv("LTS_TRASH_TTL_DAYS", "not-a-number")
    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    rc = main(["--data-dir", str(tmp_path)])
    assert rc == 1


def test_main_uses_lts_data_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No --data-dir flag → falls back to LTS_DATA_DIR."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    now = time.time()
    old = trash_dir / "old.md"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (now - 30 * 86_400, now - 30 * 86_400))

    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LTS_TRASH_TTL_DAYS", "7")
    rc = main([])
    assert rc == 0
    assert not old.exists()


def test_amain_uses_lts_trash_ttl_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LTS_TRASH_TTL_DAYS env → controls the TTL used."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    now = time.time()
    # File is 5 days old; with TTL=10 it should NOT be deleted.
    target = trash_dir / "recent.md"
    target.write_text("x", encoding="utf-8")
    os.utime(target, (now - 5 * 86_400, now - 5 * 86_400))

    monkeypatch.setenv("LTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LTS_TRASH_TTL_DAYS", "10")
    rc = main([])
    assert rc == 0
    assert target.exists(), "5-day-old file should survive TTL=10"


def test_cli_subprocess_integration(tmp_path: Path) -> None:
    """Spawn the CLI via `python -m` against a real tmpdir; verify it deletes and reports."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    now = time.time()
    old = trash_dir / "old.md"
    old.write_text("hello", encoding="utf-8")
    os.utime(old, (now - 30 * 86_400, now - 30 * 86_400))

    env = os.environ.copy()
    env["LTS_DATA_DIR"] = str(tmp_path)
    env["LTS_TRASH_TTL_DAYS"] = "7"
    env["LTS_TRASH_MAX_BYTES"] = "1073741824"  # 1 GiB; size pass is a no-op here.

    result = subprocess.run(
        [sys.executable, "-m", "local_transcription_service.retention"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert "deleted=1" in result.stdout
    assert "freed_bytes=5" in result.stdout
    assert not old.exists()

    # The structured log line (on stderr) must carry the same numbers.
    found_complete = False
    for line in result.stderr.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("event") == "retention_complete":
            found_complete = True
            assert payload["deleted"] == 1
            assert payload["kept"] == 0
            assert payload["freed_bytes"] == 5
            break
    assert found_complete, "retention_complete event missing from stderr"


def test_cli_subprocess_dry_run_does_not_delete(tmp_path: Path) -> None:
    """Spawn with --dry-run; file must remain, report must say dry_run."""
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    now = time.time()
    target = trash_dir / "old.md"
    target.write_text("x", encoding="utf-8")
    os.utime(target, (now - 30 * 86_400, now - 30 * 86_400))

    env = os.environ.copy()
    env["LTS_DATA_DIR"] = str(tmp_path)
    env["LTS_TRASH_TTL_DAYS"] = "7"

    result = subprocess.run(
        [sys.executable, "-m", "local_transcription_service.retention", "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "dry_run=True" in result.stdout
    assert target.exists(), "dry-run must not delete"


def test_cleanup_report_is_frozen() -> None:
    """CleanupReport is a frozen dataclass — assignment raises FrozenInstanceError."""
    report = CleanupReport(
        deleted=0, already_gone=0, kept=0, freed_bytes=0, dry_run=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):  # type: ignore[attr-defined]
        report.deleted = 1  # type: ignore[misc]


def test_trash_entry_is_frozen() -> None:
    """TrashEntry is a frozen dataclass."""
    e = TrashEntry(path=Path("x"), mtime=0.0, size=0)
    with pytest.raises(dataclasses.FrozenInstanceError):  # type: ignore[attr-defined]
        e.mtime = 1.0  # type: ignore[misc]


def test_retention_policy_is_frozen() -> None:
    """RetentionPolicy is a frozen dataclass."""
    p = RetentionPolicy()
    with pytest.raises(dataclasses.FrozenInstanceError):  # type: ignore[attr-defined]
        p.ttl_days = 1  # type: ignore[misc]