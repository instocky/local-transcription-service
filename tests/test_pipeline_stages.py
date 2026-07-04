"""Tests for ``pipeline/stages.py`` (Stage 1 yt-dlp + Stage 2 ffmpeg).

Subprocess and filesystem are mocked. The two real binaries
(``yt-dlp``, ``ffmpeg``) are never invoked; the fake
``_run_subprocess`` returns controlled ``_ProcResult`` values.

Coverage follows HLD-001 §12:

- yt-dlp missing / non-zero (non-network) → FETCH_FAILED, retryable=False
- yt-dlp network error                   → FETCH_FAILED, retryable=True
- yt-dlp timeout                         → FETCH_FAILED, retryable=True
- ffmpeg missing / non-zero / timeout    → AUDIO_CONDITIONING_FAILED, retryable=False

Plus argv-shape assertions (the verifier reads these to confirm
``-ar 16000 -ac 1 -c:a pcm_s16le -f wav`` is in the ffmpeg command)
and the cleanup contract (temp files removed on success and failure).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from local_transcription_service.pipeline import base, stages
from local_transcription_service.pipeline.base import PipelineError, TranscriptionPipeline
from local_transcription_service.pipeline.stages import (
    RealPipeline,
    cleanup_job_files,
    condition_audio,
    fetch_media,
)

# ---------- shared test helpers ----------


@dataclass
class _FakeProcResult:
    """Minimal subprocess-result shape consumed by stages._ProcResult.

    Using a separate dataclass keeps tests independent of the
    private ``_ProcResult`` shape, while still satisfying the
    structural contract (returncode / stdout / stderr).
    """

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class _Recorder:
    """Captures every argv passed to the fake ``_run_subprocess``.

    Lets tests assert against the *list* of subprocess calls (one
    per stage) without coupling to the underlying impl. Also lets
    a test inspect the final list of args after a whole pipeline
    run.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def make_runner(self, results: list[_FakeProcResult | BaseException]):
        """Return an async fake matching ``_run_subprocess``.

        ``results`` is consumed in order; once exhausted, the next
        call raises ``AssertionError`` (test bug, not pipeline bug).
        """

        async def fake_run(argv: list[str], **kwargs: Any) -> _FakeProcResult:
            self.calls.append((list(argv), dict(kwargs)))
            if not results:
                raise AssertionError(
                    f"fake _run_subprocess exhausted; got unexpected call {argv}"
                )
            nxt = results.pop(0)
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt

        return fake_run


class _FakeSTT:
    """In-process STTEngine for orchestrator tests.

    Records the wav_path it received, and returns a configurable
    transcript. Raises whatever ``raise`` is set to on call (lets
    us exercise Stage 3 failure paths).
    """

    def __init__(
        self,
        transcript: str = "fake transcript\n",
        raise_on_call: BaseException | None = None,
    ) -> None:
        self._transcript = transcript
        self._raise = raise_on_call
        self.calls: list[Path] = []

    async def transcribe(self, wav_path: Path, *, language: str | None = None) -> str:
        self.calls.append(wav_path)
        if self._raise is not None:
            raise self._raise
        return self._transcript

    async def is_ready(self) -> bool:  # pragma: no cover - not exercised here
        return True


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """Per-test audio cache directory under tmp_path."""
    d = tmp_path / "audio-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- Stage 1: fetch_media (yt-dlp) ----------


async def test_fetch_media_returns_produced_file_and_argv_shape(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 1 success: argv includes the contract flags and the
    resolved file path is returned.

    The fake runner writes ``{job_id}.webm`` to simulate yt-dlp's
    output before returning 0.
    """
    rec = _Recorder()
    job_id = "job-aaaa"

    def write_webm(argv, **_):
        # Simulate yt-dlp producing the file alongside the output template.
        cache_dir.joinpath(f"{job_id}.webm").write_bytes(b"raw audio")
        return _FakeProcResult(returncode=0, stderr=b"")

    rec.calls = []  # unused, but keep Recorder shape consistent
    monkeypatch.setattr(stages, "_run_subprocess", _record_then_return(rec, write_webm))

    out = await fetch_media(cache_dir, "https://www.youtube.com/watch?v=abc", job_id)
    assert out.name == f"{job_id}.webm"
    assert out.is_file()

    # argv shape: binary + flags + output template + URL, in that order.
    (argv, _kwargs) = rec.calls[0]
    assert argv[0] == "yt-dlp"
    assert "--no-playlist" in argv
    assert "--no-progress" in argv
    assert "bestaudio/best" in argv
    assert "-o" in argv
    o_index = argv.index("-o")
    assert argv[o_index + 1] == str(cache_dir / f"{job_id}.%(ext)s")
    assert argv[-1] == "https://www.youtube.com/watch?v=abc"


async def test_fetch_media_missing_binary_is_non_retryable(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yt-dlp not on PATH → FETCH_FAILED, retryable=False."""
    rec = _Recorder()

    async def fake(argv, **kwargs):
        rec.calls.append((list(argv), dict(kwargs)))
        raise FileNotFoundError(2, "No such file", "yt-dlp")

    monkeypatch.setattr(stages, "_run_subprocess", fake)

    with pytest.raises(PipelineError) as exc_info:
        await fetch_media(cache_dir, "https://youtu.be/x", "job-missing")
    assert exc_info.value.code == "FETCH_FAILED"
    assert exc_info.value.retryable is False
    assert "yt-dlp" in str(exc_info.value)
    assert rec.calls, "subprocess runner should have been called"


async def test_fetch_media_non_zero_non_network_is_non_retryable(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yt-dlp exits non-zero with no network marker → FETCH_FAILED, retryable=False.

    Simulates e.g. ``ERROR: Video unavailable`` (private / deleted).
    """
    rec = _Recorder()
    monkeypatch.setattr(
        stages,
        "_run_subprocess",
        _record_then_return(
            rec,
            _FakeProcResult(
                returncode=1,
                stderr=b"ERROR: 9j9j9j9j9j: Video unavailable",
            ),
        ),
    )

    with pytest.raises(PipelineError) as exc_info:
        await fetch_media(cache_dir, "https://youtu.be/private", "job-private")
    assert exc_info.value.code == "FETCH_FAILED"
    assert exc_info.value.retryable is False


async def test_fetch_media_network_error_is_retryable(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yt-dlp exits non-zero with a network marker → FETCH_FAILED, retryable=True."""
    rec = _Recorder()
    monkeypatch.setattr(
        stages,
        "_run_subprocess",
        _record_then_return(
            rec,
            _FakeProcResult(
                returncode=101,
                stderr=b"ERROR: unable to download webpage: <urlopen error [Errno 11001] "
                b"Temporary failure in name resolution>",
            ),
        ),
    )

    with pytest.raises(PipelineError) as exc_info:
        await fetch_media(cache_dir, "https://youtu.be/net", "job-net")
    assert exc_info.value.code == "FETCH_FAILED"
    assert exc_info.value.retryable is True
    assert "network" in str(exc_info.value).lower()


async def test_fetch_media_timeout_is_retryable(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yt-dlp hangs past the subprocess timeout → FETCH_FAILED, retryable=True."""
    rec = _Recorder()

    async def fake(argv, **kwargs):
        rec.calls.append((list(argv), dict(kwargs)))
        raise TimeoutError

    monkeypatch.setattr(stages, "_run_subprocess", fake)

    with pytest.raises(PipelineError) as exc_info:
        await fetch_media(
            cache_dir,
            "https://youtu.be/slow",
            "job-slow",
            proc_timeout_s=1.0,
        )
    assert exc_info.value.code == "FETCH_FAILED"
    assert exc_info.value.retryable is True
    assert rec.calls[0][1]["proc_timeout_s"] == 1.0


async def test_fetch_media_exit_zero_but_no_file_is_non_retryable(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yt-dlp exits 0 without producing a file → FETCH_FAILED, retryable=False.

    Catches broken yt-dlp configs / sandbox issues that succeed
    silently. The file glob must find something; empty list → fail.
    """
    rec = _Recorder()
    monkeypatch.setattr(
        stages,
        "_run_subprocess",
        _record_then_return(rec, _FakeProcResult(returncode=0, stderr=b"")),
    )

    with pytest.raises(PipelineError) as exc_info:
        await fetch_media(cache_dir, "https://youtu.be/empty", "job-empty")
    assert exc_info.value.code == "FETCH_FAILED"
    assert exc_info.value.retryable is False


async def test_fetch_media_ssl_verify_failed_is_non_retryable(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yt-dlp exits with SSL/cert error on stderr → FETCH_FAILED, retryable=False.

    Permanent operator-fixable misconfiguration (pinned cert wrong,
    system CA bundle missing, ...). Retrying with the same env fails
    the same way; the job should go to FAILED on first attempt so
    the operator notices and fixes the cert chain rather than the
    worker burning the retry budget on a doomed job.
    """
    rec = _Recorder()
    monkeypatch.setattr(
        stages,
        "_run_subprocess",
        _record_then_return(
            rec,
            _FakeProcResult(
                returncode=1,
                stderr=b"ERROR: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] "
                b"certificate verify failed: unable to get local issuer certificate>",
            ),
        ),
    )

    with pytest.raises(PipelineError) as exc_info:
        await fetch_media(cache_dir, "https://youtu.be/ssl", "job-ssl")
    assert exc_info.value.code == "FETCH_FAILED"
    assert exc_info.value.retryable is False
    assert "permanent" in str(exc_info.value).lower() or "ssl" in str(exc_info.value).lower()


async def test_fetch_media_permanent_pattern_wins_over_transient(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stderr that mentions both a permanent (SSL) and a transient
    (network) marker is classified as permanent.

    Defensive: the current pattern lists don't overlap, but if a
    future yt-dlp release bundles them on one line the classifier
    must still classify as non-retryable. Permanent is checked
    first in fetch_media.
    """
    rec = _Recorder()
    monkeypatch.setattr(
        stages,
        "_run_subprocess",
        _record_then_return(
            rec,
            _FakeProcResult(
                returncode=1,
                stderr=b"ERROR: ssl: certificate verify failed; connection reset",
            ),
        ),
    )

    with pytest.raises(PipelineError) as exc_info:
        await fetch_media(cache_dir, "https://youtu.be/both", "job-both")
    assert exc_info.value.code == "FETCH_FAILED"
    assert exc_info.value.retryable is False
    assert exc_info.value.retryable is False


# ---------- Stage 2: condition_audio (ffmpeg) ----------


async def test_condition_audio_returns_wav_path_with_contract_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 2 success: argv includes -ar 16000 -ac 1 -c:a pcm_s16le -f wav."""
    rec = _Recorder()
    raw = tmp_path / "input.webm"
    raw.write_bytes(b"raw")
    wav = tmp_path / "out.wav"

    def make_wav(argv, **_):
        # Simulate ffmpeg producing the wav.
        wav.write_bytes(b"RIFF....WAVE")
        return _FakeProcResult(returncode=0, stderr=b"")

    monkeypatch.setattr(stages, "_run_subprocess", _record_then_return(rec, make_wav))

    out = await condition_audio(raw, wav)
    assert out == wav
    assert wav.is_file()

    (argv, _kwargs) = rec.calls[0]
    assert argv[0] == "ffmpeg"
    # Contract: Whisper-compatible WAV flags must all be present.
    for flag in ("-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav"):
        assert flag in argv, f"missing {flag!r} in ffmpeg argv {argv!r}"
    # And the input/output positions.
    assert "-i" in argv
    i_idx = argv.index("-i")
    assert argv[i_idx + 1] == str(raw)
    assert argv[-1] == str(wav)


async def test_condition_audio_missing_binary_is_non_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg not on PATH → AUDIO_CONDITIONING_FAILED, retryable=False."""
    rec = _Recorder()

    async def fake(argv, **kwargs):
        rec.calls.append((list(argv), dict(kwargs)))
        raise FileNotFoundError(2, "No such file", "ffmpeg")

    monkeypatch.setattr(stages, "_run_subprocess", fake)

    with pytest.raises(PipelineError) as exc_info:
        await condition_audio(tmp_path / "in", tmp_path / "out.wav")
    assert exc_info.value.code == "AUDIO_CONDITIONING_FAILED"
    assert exc_info.value.retryable is False
    assert "ffmpeg" in str(exc_info.value)


async def test_condition_audio_non_zero_is_non_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg exits non-zero → AUDIO_CONDITIONING_FAILED, retryable=False."""
    rec = _Recorder()
    monkeypatch.setattr(
        stages,
        "_run_subprocess",
        _record_then_return(
            rec,
            _FakeProcResult(
                returncode=1,
                stderr=b"Invalid data found when processing input",
            ),
        ),
    )

    with pytest.raises(PipelineError) as exc_info:
        await condition_audio(tmp_path / "in", tmp_path / "out.wav")
    assert exc_info.value.code == "AUDIO_CONDITIONING_FAILED"
    assert exc_info.value.retryable is False


async def test_condition_audio_timeout_is_non_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg hangs past the subprocess timeout → AUDIO_CONDITIONING_FAILED."""
    rec = _Recorder()

    async def fake(argv, **kwargs):
        rec.calls.append((list(argv), dict(kwargs)))
        raise TimeoutError

    monkeypatch.setattr(stages, "_run_subprocess", fake)

    with pytest.raises(PipelineError) as exc_info:
        await condition_audio(
            tmp_path / "in",
            tmp_path / "out.wav",
            proc_timeout_s=1.0,
        )
    assert exc_info.value.code == "AUDIO_CONDITIONING_FAILED"
    assert exc_info.value.retryable is False


async def test_condition_audio_exit_zero_but_no_wav_is_non_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg exits 0 but the wav file is missing → AUDIO_CONDITIONING_FAILED."""
    rec = _Recorder()
    monkeypatch.setattr(
        stages,
        "_run_subprocess",
        _record_then_return(rec, _FakeProcResult(returncode=0, stderr=b"")),
    )

    out_wav = tmp_path / "never_created.wav"
    with pytest.raises(PipelineError) as exc_info:
        await condition_audio(tmp_path / "in", out_wav)
    assert exc_info.value.code == "AUDIO_CONDITIONING_FAILED"
    assert exc_info.value.retryable is False
    assert not out_wav.exists()


# ---------- cleanup helper ----------


async def test_cleanup_job_files_removes_prefixed_files(
    cache_dir: Path,
) -> None:
    cache_dir.joinpath("job-1.webm").write_bytes(b"x")
    cache_dir.joinpath("job-1.wav").write_bytes(b"x")
    cache_dir.joinpath("job-2.webm").write_bytes(b"x")
    cache_dir.joinpath("other-job.webm").write_bytes(b"x")

    removed = cleanup_job_files(cache_dir, "job-1")
    assert removed == 2
    assert not cache_dir.joinpath("job-1.webm").exists()
    assert not cache_dir.joinpath("job-1.wav").exists()
    assert cache_dir.joinpath("job-2.webm").exists()
    assert cache_dir.joinpath("other-job.webm").exists()


def test_cleanup_job_files_handles_missing_dir(tmp_path: Path) -> None:
    """Missing cache dir is a no-op (Stage 1 may fail before mkdir)."""
    assert cleanup_job_files(tmp_path / "nonexistent", "job-x") == 0


def test_cleanup_job_files_returns_zero_when_no_match(
    cache_dir: Path,
) -> None:
    cache_dir.joinpath("unrelated.webm").write_bytes(b"x")
    assert cleanup_job_files(cache_dir, "job-not-here") == 0


# ---------- RealPipeline composition ----------


async def test_real_pipeline_runs_stages_and_cleans_up_on_success(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success path: Stage 1 + Stage 2 run, STTEngine.transcribe called,
    ALL {job_id}.* files gone afterwards (HLD-001 §11)."""
    rec = _Recorder()
    job_id = "job-success"
    stt = _FakeSTT(transcript="hello world\n")

    def fetch(argv, **_):
        cache_dir.joinpath(f"{job_id}.webm").write_bytes(b"raw")
        return _FakeProcResult(returncode=0, stderr=b"")

    def condition(argv, **_):
        cache_dir.joinpath(f"{job_id}.wav").write_bytes(b"wav")
        return _FakeProcResult(returncode=0, stderr=b"")

    monkeypatch.setattr(stages, "_run_subprocess", _two_stage_runner(rec, fetch, condition))

    pipeline = RealPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    result = await pipeline.transcribe("https://youtu.be/ok", job_id=job_id)

    assert result == "hello world\n"
    assert len(stt.calls) == 1
    assert stt.calls[0] == cache_dir / f"{job_id}.wav"

    # Two subprocess calls: yt-dlp, then ffmpeg.
    assert len(rec.calls) == 2
    assert rec.calls[0][0][0] == "yt-dlp"
    assert rec.calls[1][0][0] == "ffmpeg"

    # Cleanup: no {job_id}.* files left behind.
    assert not list(cache_dir.glob(f"{job_id}.*"))  # noqa: ASYNC240


async def test_real_pipeline_cleans_up_on_stage1_failure(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 1 failure: cleanup still runs, exception propagates with correct code."""
    rec = _Recorder()
    job_id = "job-stage1-fail"
    stt = _FakeSTT()

    async def fetch_fail(argv, **kwargs):
        rec.calls.append((list(argv), dict(kwargs)))
        raise FileNotFoundError(2, "No such file", "yt-dlp")

    monkeypatch.setattr(stages, "_run_subprocess", fetch_fail)

    pipeline = RealPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    with pytest.raises(PipelineError) as exc_info:
        await pipeline.transcribe("https://youtu.be/x", job_id=job_id)
    assert exc_info.value.code == "FETCH_FAILED"
    assert exc_info.value.retryable is False
    assert stt.calls == [], "Stage 3 must NOT be invoked when Stage 1 fails"
    assert not list(cache_dir.glob(f"{job_id}.*"))  # noqa: ASYNC240


async def test_real_pipeline_cleans_up_on_stage2_failure(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 2 failure: the raw file from Stage 1 must be cleaned up."""
    rec = _Recorder()
    job_id = "job-stage2-fail"
    stt = _FakeSTT()

    def fetch(argv, **_):
        cache_dir.joinpath(f"{job_id}.webm").write_bytes(b"raw")
        return _FakeProcResult(returncode=0, stderr=b"")

    def condition_fail(argv, **kwargs):
        rec.calls.append((list(argv), dict(kwargs)))
        raise FileNotFoundError(2, "No such file", "ffmpeg")

    monkeypatch.setattr(stages, "_run_subprocess", _two_stage_runner(rec, fetch, condition_fail))

    pipeline = RealPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    with pytest.raises(PipelineError) as exc_info:
        await pipeline.transcribe("https://youtu.be/x", job_id=job_id)
    assert exc_info.value.code == "AUDIO_CONDITIONING_FAILED"
    assert stt.calls == [], "Stage 3 must NOT be invoked when Stage 2 fails"
    # The raw file from Stage 1 is cleaned up too (the orchestrator
    # cleans everything matching {job_id}.*).
    assert not list(cache_dir.glob(f"{job_id}.*"))  # noqa: ASYNC240


async def test_real_pipeline_cleans_up_on_stage3_failure(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 3 failure: WAV and raw file both cleaned; STT exception propagates."""
    rec = _Recorder()
    job_id = "job-stage3-fail"
    stt_err = PipelineError("model not registered", code="MODEL_NOT_PULLED", retryable=False)
    stt = _FakeSTT(raise_on_call=stt_err)

    def fetch(argv, **_):
        cache_dir.joinpath(f"{job_id}.webm").write_bytes(b"raw")
        return _FakeProcResult(returncode=0, stderr=b"")

    def condition(argv, **_):
        cache_dir.joinpath(f"{job_id}.wav").write_bytes(b"wav")
        return _FakeProcResult(returncode=0, stderr=b"")

    monkeypatch.setattr(stages, "_run_subprocess", _two_stage_runner(rec, fetch, condition))

    pipeline = RealPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    with pytest.raises(PipelineError) as exc_info:
        await pipeline.transcribe("https://youtu.be/x", job_id=job_id)
    # The STT engine's own code propagates unchanged — the
    # orchestrator does not wrap or remap it.
    assert exc_info.value.code == "MODEL_NOT_PULLED"
    assert exc_info.value.retryable is False
    assert not list(cache_dir.glob(f"{job_id}.*"))  # noqa: ASYNC240


async def test_real_pipeline_creates_cache_dir_if_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``audio_cache_dir`` is auto-created (HLD-001 §11 / §13).

    Tests don't want to depend on the worker calling
    ``settings.ensure_dirs()`` first.
    """
    cache = tmp_path / "fresh" / "audio-cache"
    assert not cache.exists()
    rec = _Recorder()
    job_id = "job-mkdir"
    stt = _FakeSTT(transcript="ok\n")

    def fetch(argv, **_):
        cache.joinpath(f"{job_id}.webm").write_bytes(b"raw")
        return _FakeProcResult(returncode=0, stderr=b"")

    def condition(argv, **_):
        cache.joinpath(f"{job_id}.wav").write_bytes(b"wav")
        return _FakeProcResult(returncode=0, stderr=b"")

    monkeypatch.setattr(stages, "_run_subprocess", _two_stage_runner(rec, fetch, condition))

    pipeline = RealPipeline(stt_engine=stt, audio_cache_dir=cache)
    assert await pipeline.transcribe("https://youtu.be/mkdir", job_id=job_id) == "ok\n"
    assert cache.is_dir()


# ---------- helpers used by the composition tests ----------


def _record_then_return(rec: _Recorder, result_or_fn: Any):
    """Build a fake ``_run_subprocess`` that records then returns one result.

    ``result_or_fn`` can be a ``_FakeProcResult`` (returned as-is) or
    a callable ``(argv, **kwargs) -> _FakeProcResult`` (lets the test
    write side-effect files based on the argv before returning).
    Sync and async callables are both accepted — async results are
    awaited transparently.
    """
    import inspect

    async def fake(argv: list[str], **kwargs: Any) -> _FakeProcResult:
        rec.calls.append((list(argv), dict(kwargs)))
        if callable(result_or_fn):
            out = result_or_fn(argv, **kwargs)
            if inspect.iscoroutine(out):
                out = await out
            return out
        return result_or_fn

    return fake


def _two_stage_runner(
    rec: _Recorder, fetch_fn: Any, condition_fn: Any
):
    """Fake ``_run_subprocess`` that alternates fetch → condition.

    The orchestrator calls ``_run_subprocess`` exactly twice in the
    success path: once for yt-dlp, once for ffmpeg. This helper
    dispatches by argv[0] (the binary name).
    """
    import inspect

    async def fake(argv: list[str], **kwargs: Any) -> _FakeProcResult:
        rec.calls.append((list(argv), dict(kwargs)))
        binary = argv[0]
        if "yt-dlp" in binary:
            fn = fetch_fn
        elif "ffmpeg" in binary:
            fn = condition_fn
        else:
            raise AssertionError(f"unexpected binary in argv: {binary!r}")
        out = fn(argv, **kwargs)
        if inspect.iscoroutine(out):
            out = await out
        return out

    return fake


# ---------- subprocess-invocation shape (verifier check #3) ----------


async def test_subprocess_runner_uses_create_subprocess_exec_not_shell() -> None:
    """Sanity check on the helper's own source: ``create_subprocess_exec``
    is used (no shell), ``create_subprocess_shell`` is never invoked.

    Imported for ``inspect`` introspection so the assertion stays
    meaningful even after a future refactor that wraps the call.
    """
    import inspect

    source = inspect.getsource(stages._run_subprocess)
    assert "create_subprocess_exec" in source
    assert "create_subprocess_shell" not in source
    assert "shell=True" not in source

    # Also confirm the module exposes no public shell helper at all.
    for name in dir(stages):
        if name.startswith("_"):
            continue
        assert "shell" not in name.lower(), f"public shell-related symbol: {name}"


# ---------- cross-references ----------


def test_pipeline_error_docstring_lists_canonical_codes() -> None:
    """The base.py docstring must reference HLD-canonical codes only
    (FETCH_FAILED, AUDIO_CONDITIONING_FAILED, MODEL_NOT_PULLED); the
    old PIPELINE_TRANSIENT / INVALID_URL / MODEL_MISSING labels are
    gone from the docstring."""
    doc = base.PipelineError.__doc__ or ""
    assert "FETCH_FAILED" in doc
    assert "AUDIO_CONDITIONING_FAILED" in doc
    assert "MODEL_NOT_PULLED" in doc
    for legacy in ("INVALID_URL", "MODEL_MISSING"):
        assert legacy not in doc, f"legacy code {legacy!r} still in docstring"
    # Note: PIPELINE_TRANSIENT survives only as a worker-side fallback
    # (worker._error_from_exception); it must NOT be advertised as a
    # canonical PipelineError code here.
    assert "PIPELINE_TRANSIENT" not in doc


def test_base_transcribe_signature_threads_job_id() -> None:
    """``TranscriptionPipeline.transcribe`` must accept ``job_id``
    keyword-only (the orchestrator uses it to scope temp files)."""
    import inspect

    sig = inspect.signature(TranscriptionPipeline.transcribe)
    params = sig.parameters
    assert "video_url" in params
    assert "job_id" in params
    job_id_param = params["job_id"]
    assert job_id_param.kind is inspect.Parameter.KEYWORD_ONLY
    # ``from __future__ import annotations`` makes annotations strings
    # at runtime; compare against the string form.
    assert job_id_param.annotation == "str"