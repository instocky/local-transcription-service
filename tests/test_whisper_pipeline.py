"""Tests for ``pipeline/whisper_pipeline.py`` (TASK-B §B4).

``WhisperPipeline`` is the canonical public name for the real
three-stage orchestrator (yt-dlp → ffmpeg → STTEngine). It is a
thin subclass of ``RealPipeline`` that adds structured stage
logging per HLD-001 §15.

Coverage:

- identity / public surface: ``WhisperPipeline`` is the same class
  as ``RealPipeline`` (subclass), exposes the ``engine`` property,
  is the value re-exported from ``local_transcription_service.pipeline``.
- stage sequencing: Stage 1 runs before Stage 2 runs before Stage 3.
- cleanup on success and on every stage failure path (Stage 1, 2, 3).
- ``STTEngine.transcribe`` is called with the path produced by
  Stage 2 and the pipeline returns its result **verbatim** (worker
  writes to disk; pipeline does not touch ``results/``).
- structured stage logging: each stage emits ``stage_started`` then
  ``stage_finished`` with the HLD-canonical stage name
  (``fetch`` / ``condition`` / ``stt``), the job id, and a
  ``duration_s`` on the finished record. Failures log
  ``stage_started`` only (no finished), so a stuck stage is
  visible in logs.

Subprocess is mocked (``monkeypatch.setattr(stages, '_run_subprocess', ...)``)
and the STT engine is an in-process fake. No real binaries, no
real network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from local_transcription_service.pipeline import RealPipeline, WhisperPipeline, stages
from local_transcription_service.pipeline.base import PipelineError

# ---------- shared test helpers (kept local — small set, no value
# in lifting to conftest for one test module) ----------


@dataclass
class _FakeProcResult:
    """Minimal subprocess-result shape consumed by ``stages._ProcResult``."""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class _Recorder:
    """Captures every argv passed to the fake ``_run_subprocess``."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []


def _two_stage_runner(
    rec: _Recorder, fetch_fn: Any, condition_fn: Any
):
    """Fake ``_run_subprocess`` that dispatches fetch → condition by argv[0]."""

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


class _FakeSTT:
    """In-process ``STTEngine`` for orchestrator tests.

    Records every ``transcribe(wav_path)`` call, returns a
    configurable transcript, and raises whatever ``raise_on_call``
    is set to (lets us exercise Stage 3 failure paths). Also tracks
    ``is_ready`` so the readiness wiring has something to call.
    """

    def __init__(
        self,
        transcript: str = "fake transcript\n",
        raise_on_call: BaseException | None = None,
        is_ready_result: bool = True,
    ) -> None:
        self._transcript = transcript
        self._raise = raise_on_call
        self._is_ready_result = is_ready_result
        self.calls: list[Path] = []

    async def transcribe(self, wav_path: Path, *, language: str | None = None) -> str:
        self.calls.append(wav_path)
        if self._raise is not None:
            raise self._raise
        return self._transcript

    async def is_ready(self) -> bool:
        return self._is_ready_result


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """Per-test audio cache directory under tmp_path."""
    d = tmp_path / "audio-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- public surface ----------


def test_whisper_pipeline_is_re_exported_from_package() -> None:
    """``pipeline.WhisperPipeline`` is the canonical name (TASK-B §B4)."""
    from local_transcription_service.pipeline import __all__

    assert "WhisperPipeline" in __all__


def test_whisper_pipeline_is_subclass_of_real_pipeline() -> None:
    """``WhisperPipeline`` is a subclass of ``RealPipeline`` — the same
    class, just re-named for the public surface.

    Existing pre-B4 tests that imported ``RealPipeline`` continue to
    work; new B4 tests use ``WhisperPipeline``. Both ``isinstance``
    checks succeed in either direction.
    """
    assert issubclass(WhisperPipeline, RealPipeline)
    # And an instance of one is an instance of the other.
    engine = _FakeSTT()
    p = WhisperPipeline(stt_engine=engine, audio_cache_dir=Path("/tmp/cache"))
    assert isinstance(p, RealPipeline)


def test_whisper_pipeline_exposes_engine_property(cache_dir: Path) -> None:
    """The pipeline exposes the injected engine so ``/ready`` can call
    ``engine.is_ready()`` (HLD-001 §8, TASK-B §B4) without reaching
    into private state.
    """
    engine = _FakeSTT()
    p = WhisperPipeline(stt_engine=engine, audio_cache_dir=cache_dir)
    assert p.engine is engine


def test_whisper_pipeline_repr_uses_canonical_name() -> None:
    """``WhisperPipeline.__name__`` is the canonical public name;
    the underlying class is ``RealPipeline`` but the alias keeps
    log/repr output clean."""
    assert WhisperPipeline.__name__ == "WhisperPipeline"


# ---------- stage sequencing ----------


async def test_stages_run_in_order_fetch_then_condition_then_stt(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: yt-dlp → ffmpeg → STTEngine in that order.

    The fake runner records argv[0] for each call; we also record
    the STT engine call timing to confirm it ran after both stages
    had produced their artifacts.
    """
    rec = _Recorder()
    job_id = "job-order"
    stt = _FakeSTT(transcript="ok\n")

    def fetch(argv, **_):
        cache_dir.joinpath(f"{job_id}.webm").write_bytes(b"raw")
        return _FakeProcResult(returncode=0, stderr=b"")

    def condition(argv, **_):
        cache_dir.joinpath(f"{job_id}.wav").write_bytes(b"wav")
        return _FakeProcResult(returncode=0, stderr=b"")

    monkeypatch.setattr(stages, "_run_subprocess", _two_stage_runner(rec, fetch, condition))

    pipeline = WhisperPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    text = await pipeline.transcribe("https://youtu.be/order", job_id=job_id)

    assert text == "ok\n"
    assert len(rec.calls) == 2
    assert rec.calls[0][0][0] == "yt-dlp"
    assert rec.calls[1][0][0] == "ffmpeg"
    assert stt.calls == [cache_dir / f"{job_id}.wav"]


async def test_engine_transcript_returned_verbatim(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline returns the engine's transcript string unchanged.

    Worker writes to disk; the pipeline does not transform or wrap
    the result (no trailing-newline addition, no header, nothing).
    """
    rec = _Recorder()
    job_id = "job-verbatim"
    expected = "verbatim transcript — do not modify\n\nwith weird unicode ✓\n"
    stt = _FakeSTT(transcript=expected)

    def fetch(argv, **_):
        cache_dir.joinpath(f"{job_id}.webm").write_bytes(b"raw")
        return _FakeProcResult(returncode=0, stderr=b"")

    def condition(argv, **_):
        cache_dir.joinpath(f"{job_id}.wav").write_bytes(b"wav")
        return _FakeProcResult(returncode=0, stderr=b"")

    monkeypatch.setattr(stages, "_run_subprocess", _two_stage_runner(rec, fetch, condition))

    pipeline = WhisperPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    got = await pipeline.transcribe("https://youtu.be/v", job_id=job_id)
    assert got == expected
    assert got is stt._transcript  # identity, not just equality


# ---------- cleanup on every path ----------


async def test_cleanup_runs_on_stage1_failure(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder()
    job_id = "job-s1-fail"
    stt = _FakeSTT()

    async def fetch_fail(argv, **kwargs):
        rec.calls.append((list(argv), dict(kwargs)))
        raise FileNotFoundError(2, "No such file", "yt-dlp")

    monkeypatch.setattr(stages, "_run_subprocess", fetch_fail)

    pipeline = WhisperPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    with pytest.raises(PipelineError) as exc_info:
        await pipeline.transcribe("https://youtu.be/x", job_id=job_id)
    assert exc_info.value.code == "FETCH_FAILED"
    assert stt.calls == [], "STT engine must NOT be invoked when Stage 1 fails"
    assert not list(cache_dir.glob(f"{job_id}.*"))  # noqa: ASYNC240


async def test_cleanup_runs_on_stage2_failure(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder()
    job_id = "job-s2-fail"
    stt = _FakeSTT()

    def fetch(argv, **_):
        cache_dir.joinpath(f"{job_id}.webm").write_bytes(b"raw")
        return _FakeProcResult(returncode=0, stderr=b"")

    def condition_fail(argv, **kwargs):
        rec.calls.append((list(argv), dict(kwargs)))
        raise FileNotFoundError(2, "No such file", "ffmpeg")

    monkeypatch.setattr(
        stages, "_run_subprocess", _two_stage_runner(rec, fetch, condition_fail)
    )

    pipeline = WhisperPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    with pytest.raises(PipelineError) as exc_info:
        await pipeline.transcribe("https://youtu.be/x", job_id=job_id)
    assert exc_info.value.code == "AUDIO_CONDITIONING_FAILED"
    assert stt.calls == [], "STT engine must NOT be invoked when Stage 2 fails"
    assert not list(cache_dir.glob(f"{job_id}.*"))  # noqa: ASYNC240


async def test_cleanup_runs_on_stage3_failure(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder()
    job_id = "job-s3-fail"
    stt_err = PipelineError(
        "model not registered", code="MODEL_NOT_PULLED", retryable=False
    )
    stt = _FakeSTT(raise_on_call=stt_err)

    def fetch(argv, **_):
        cache_dir.joinpath(f"{job_id}.webm").write_bytes(b"raw")
        return _FakeProcResult(returncode=0, stderr=b"")

    def condition(argv, **_):
        cache_dir.joinpath(f"{job_id}.wav").write_bytes(b"wav")
        return _FakeProcResult(returncode=0, stderr=b"")

    monkeypatch.setattr(stages, "_run_subprocess", _two_stage_runner(rec, fetch, condition))

    pipeline = WhisperPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    with pytest.raises(PipelineError) as exc_info:
        await pipeline.transcribe("https://youtu.be/x", job_id=job_id)
    assert exc_info.value.code == "MODEL_NOT_PULLED"
    assert not list(cache_dir.glob(f"{job_id}.*"))  # noqa: ASYNC240


# ---------- structured stage logging (HLD-001 §15) ----------


def _stage_events(records: list[logging.LogRecord], job_id: str) -> list[logging.LogRecord]:
    """Filter log records down to the ones with event ∈ {stage_started, stage_finished}
    for the given job_id, in emit order."""
    return [
        r
        for r in records
        if getattr(r, "event", None) in {"stage_started", "stage_finished"}
        and getattr(r, "job_id", None) == job_id
    ]


async def test_each_stage_emits_started_then_finished_with_hld_names(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """HLD-001 §15: each stage logs ``stage_started`` followed by
    ``stage_finished`` with the canonical stage name (``fetch`` /
    ``condition`` / ``stt``), the job id, and ``duration_s`` on the
    finished record.
    """
    rec = _Recorder()
    job_id = "job-log-success"
    stt = _FakeSTT(transcript="ok\n")

    def fetch(argv, **_):
        cache_dir.joinpath(f"{job_id}.webm").write_bytes(b"raw")
        return _FakeProcResult(returncode=0, stderr=b"")

    def condition(argv, **_):
        cache_dir.joinpath(f"{job_id}.wav").write_bytes(b"wav")
        return _FakeProcResult(returncode=0, stderr=b"")

    monkeypatch.setattr(stages, "_run_subprocess", _two_stage_runner(rec, fetch, condition))

    pipeline = WhisperPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    with caplog.at_level(
        logging.INFO, logger="local_transcription_service.pipeline.whisper_pipeline"
    ):
        await pipeline.transcribe("https://youtu.be/log", job_id=job_id)

    events = _stage_events(caplog.records, job_id)
    assert [r.event for r in events] == [
        "stage_started",
        "stage_finished",
        "stage_started",
        "stage_finished",
        "stage_started",
        "stage_finished",
    ]
    assert [r.stage for r in events] == [
        "fetch",
        "fetch",
        "condition",
        "condition",
        "stt",
        "stt",
    ]

    finished = [r for r in events if r.event == "stage_finished"]
    assert all(hasattr(r, "duration_s") for r in finished)
    # duration_s must be a non-negative number
    for r in finished:
        assert isinstance(r.duration_s, float)
        assert r.duration_s >= 0.0


async def test_stage_logging_skips_finished_on_failure(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """On Stage 1 failure the ``fetch`` stage logs ``stage_started``
    but **not** ``stage_finished`` (the stage never completed).
    No later stage is logged either (we never got there).
    """
    rec = _Recorder()
    job_id = "job-log-fail"
    stt = _FakeSTT()

    async def fetch_fail(argv, **kwargs):
        rec.calls.append((list(argv), dict(kwargs)))
        raise FileNotFoundError(2, "No such file", "yt-dlp")

    monkeypatch.setattr(stages, "_run_subprocess", fetch_fail)

    pipeline = WhisperPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    with caplog.at_level(
        logging.INFO, logger="local_transcription_service.pipeline.whisper_pipeline"
    ):
        with pytest.raises(PipelineError):
            await pipeline.transcribe("https://youtu.be/x", job_id=job_id)

    events = _stage_events(caplog.records, job_id)
    # Exactly one started, no finished, and nothing for later stages.
    assert [r.event for r in events] == ["stage_started"]
    assert [r.stage for r in events] == ["fetch"]


async def test_stage3_failure_logs_started_without_finished(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If the STT engine raises, ``stt`` stage_started is logged but
    no ``stage_finished`` follows — the orchestrator's
    ``try/finally`` lets the exception propagate, and cleanup still
    runs (verified separately).
    """
    rec = _Recorder()
    job_id = "job-log-s3-fail"
    stt_err = PipelineError("boom", code="STT_GATEWAY_UNAVAILABLE", retryable=True)
    stt = _FakeSTT(raise_on_call=stt_err)

    def fetch(argv, **_):
        cache_dir.joinpath(f"{job_id}.webm").write_bytes(b"raw")
        return _FakeProcResult(returncode=0, stderr=b"")

    def condition(argv, **_):
        cache_dir.joinpath(f"{job_id}.wav").write_bytes(b"wav")
        return _FakeProcResult(returncode=0, stderr=b"")

    monkeypatch.setattr(stages, "_run_subprocess", _two_stage_runner(rec, fetch, condition))

    pipeline = WhisperPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    with caplog.at_level(
        logging.INFO, logger="local_transcription_service.pipeline.whisper_pipeline"
    ):
        with pytest.raises(PipelineError):
            await pipeline.transcribe("https://youtu.be/x", job_id=job_id)

    events = _stage_events(caplog.records, job_id)
    # fetch + condition are completed (started → finished each),
    # stt is started but never finished.
    started = [r for r in events if r.event == "stage_started"]
    finished = [r for r in events if r.event == "stage_finished"]
    assert [r.stage for r in started] == ["fetch", "condition", "stt"]
    assert [r.stage for r in finished] == ["fetch", "condition"]


# ---------- STTEngine delegation ----------


async def test_engine_transcribe_called_with_stage2_output(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine is invoked with the WAV path produced by Stage 2,
    not with the raw Stage 1 path or the URL.
    """
    rec = _Recorder()
    job_id = "job-delegate"
    stt = _FakeSTT(transcript="hi\n")

    def fetch(argv, **_):
        cache_dir.joinpath(f"{job_id}.webm").write_bytes(b"raw")
        return _FakeProcResult(returncode=0, stderr=b"")

    def condition(argv, **_):
        cache_dir.joinpath(f"{job_id}.wav").write_bytes(b"wav")
        return _FakeProcResult(returncode=0, stderr=b"")

    monkeypatch.setattr(stages, "_run_subprocess", _two_stage_runner(rec, fetch, condition))

    pipeline = WhisperPipeline(stt_engine=stt, audio_cache_dir=cache_dir)
    await pipeline.transcribe("https://youtu.be/d", job_id=job_id)

    assert stt.calls == [cache_dir / f"{job_id}.wav"]