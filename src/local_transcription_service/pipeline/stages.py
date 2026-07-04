"""Real Stage 1 (yt-dlp) + Stage 2 (ffmpeg) + ``RealPipeline`` orchestrator.

The pipeline is split into three stages per HLD-001 §11:

- **Stage 1** — media acquisition: ``yt-dlp`` downloads the source
  media into ``${audio_cache_dir}/{job_id}.{ext}``.
- **Stage 2** — audio conditioning: ``ffmpeg`` normalises it to
  16 kHz mono PCM WAV (the format whisper.cpp expects) at
  ``${audio_cache_dir}/{job_id}.wav``.
- **Stage 3** — speech-to-text: delegated to an injected
  ``STTEngine`` (see protocol below).

``RealPipeline.transcribe(video_url, *, job_id)`` runs the three
stages sequentially and removes every ``audio-cache/{job_id}.*``
file on both success and failure via a ``try/finally`` block. The
worker writes the returned text to ``results/{job_id}.md``; the
pipeline does NOT touch ``results/`` or the job store.

Error codes follow HLD-001 §12:

- ``yt-dlp`` missing / non-zero (non-network) → ``FETCH_FAILED``,
  ``retryable=False``.
- ``yt-dlp`` exits with a permanent marker on stderr (SSL/cert,
  client-cert load failure, ...) → ``FETCH_FAILED``,
  ``retryable=False``. Retrying won't help until the operator
  fixes the underlying configuration.
- ``yt-dlp`` exits with a transient network marker on stderr →
  ``FETCH_FAILED``, ``retryable=True`` (so a transient drop is
  retried after the §10 backoff).
- ``ffmpeg`` missing / non-zero / timeout → ``AUDIO_CONDITIONING_FAILED``,
  ``retryable=False``.

Subprocess invocation goes through ``asyncio.create_subprocess_exec``
(no shell, no string-concatenated argv) via the ``_run_subprocess``
helper, which is the single injection seam for tests
(``monkeypatch`` it on the module to return controlled results).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from local_transcription_service.pipeline.base import PipelineError, TranscriptionPipeline
from local_transcription_service.stt.base import STTEngine

logger = logging.getLogger(__name__)


# ---------- tunables ----------

#: Default subprocess timeout for ``yt-dlp``. Generous enough for slow
#: connections on the Mac Mini LAN; tight enough that a stuck
#: download doesn't pin a worker forever.
_FETCH_TIMEOUT_S: float = 300.0

#: Default subprocess timeout for ``ffmpeg``. 16 kHz mono conversion
#: of a 60-minute input is sub-second on Apple Silicon.
_CONDITION_TIMEOUT_S: float = 120.0

#: Substrings in yt-dlp stderr that indicate a transient network
#: problem. Matched case-insensitively against the joined stderr
#: text. The list is conservative — false positives (a wrongly-
#: retried "network" error) are cheap; a wrongly-swallowed permanent
#: error would be worse.
_NETWORK_ERROR_PATTERNS: tuple[str, ...] = (
    "unable to download webpage",
    "unable to extract",
    "http error",
    "temporary failure in name resolution",
    "network is unreachable",
    "connection refused",
    "connection reset",
    "connection aborted",
    "remote end closed connection",
    "no address associated with hostname",
    "could not resolve host",
)

#: Substrings in yt-dlp stderr that indicate a permanent,
#: operator-fixable error. Retrying will not help — these are
#: configuration or environment mistakes that need a human
#: (install the right CA bundle, fix the proxy allow-list, ...).
#: Matched case-insensitively against the joined stderr text.
#: Checked BEFORE the network list so a substring that matches
#: both (none today) is classified as permanent.
_PERMANENT_ERROR_PATTERNS: tuple[str, ...] = (
    # SSL / TLS — pinned cert wrong, system CA bundle missing,
    # self-signed cert in proxy chain, etc. Retrying with the same
    # env will fail the same way until the operator fixes it.
    "ssl: certificate verify failed",
    "certificate verify failed",
    "unable to load client certificate",
)


# ---------- subprocess runner (test injection seam) ----------


@dataclass(frozen=True)
class _ProcResult:
    """Outcome of a single subprocess invocation."""

    returncode: int
    stdout: bytes
    stderr: bytes


async def _run_subprocess(argv: list[str], *, proc_timeout_s: float) -> _ProcResult:
    """Spawn ``argv`` via :func:`asyncio.create_subprocess_exec` and await it.

    No shell, no string concatenation of argv — each element is a
    discrete argument to the OS. ``FileNotFoundError`` propagates
    (binary missing on ``$PATH``); ``asyncio.TimeoutError`` propagates
    (after killing the process).

    This is the single injection seam for tests. Replace this
    attribute on the module under test with a fake that returns a
    controlled ``_ProcResult`` (or raises the exception you want
    to simulate) to exercise the stages without real binaries.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=proc_timeout_s
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return _ProcResult(returncode=proc.returncode, stdout=stdout_b, stderr=stderr_b)


# ---------- Stage 1: media acquisition (yt-dlp) ----------


async def fetch_media(
    cache_dir: Path,
    video_url: str,
    job_id: str,
    *,
    ytdlp_bin: str = "yt-dlp",
    proc_timeout_s: float = _FETCH_TIMEOUT_S,
) -> Path:
    """Stage 1 — download the source media via ``yt-dlp`` (HLD-001 §11).

    Output template is ``{cache_dir}/{job_id}.%(ext)s``; the actual
    extension is decided by yt-dlp at runtime. Returns the resolved
    path on success.

    Errors (HLD-001 §12):

    - ``yt-dlp`` missing on ``$PATH`` → non-retryable ``FETCH_FAILED``.
    - ``yt-dlp`` exits non-zero, no network / permanent marker on
      stderr → non-retryable ``FETCH_FAILED``.
    - ``yt-dlp`` exits non-zero, stderr matches a permanent marker
      (SSL/cert, ...) → non-retryable ``FETCH_FAILED``.
    - ``yt-dlp`` exits non-zero, stderr matches a transient network
      marker → retryable ``FETCH_FAILED``.
    - ``yt-dlp`` times out → retryable ``FETCH_FAILED``.
    - ``yt-dlp`` exits 0 but produced no matching file →
      non-retryable ``FETCH_FAILED``.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    output_template = str(cache_dir / f"{job_id}.%(ext)s")
    argv = [
        ytdlp_bin,
        "--no-playlist",
        "--no-progress",
        "--no-part",
        "--no-mtime",
        "-f",
        "bestaudio/best",
        "-o",
        output_template,
        video_url,
    ]
    try:
        result = await _run_subprocess(argv, proc_timeout_s=proc_timeout_s)
    except FileNotFoundError as exc:
        msg = f"yt-dlp binary not found on PATH (looked for {ytdlp_bin!r})"
        raise PipelineError(msg, code="FETCH_FAILED", retryable=False) from exc
    except TimeoutError as exc:
        msg = f"yt-dlp timed out after {proc_timeout_s}s for {video_url}"
        raise PipelineError(msg, code="FETCH_FAILED", retryable=True) from exc

    if result.returncode != 0:
        stderr_text = result.stderr.decode("utf-8", errors="replace").lower()
        # Permanent errors are checked first — they are operator-fixable
        # misconfigurations (SSL/cert, ...) and retrying them just burns
        # the attempt budget. The network list is the catch-all for
        # transient problems that have a chance of clearing on retry.
        if any(p in stderr_text for p in _PERMANENT_ERROR_PATTERNS):
            snippet = result.stderr.decode("utf-8", errors="replace")[:500]
            msg = f"yt-dlp permanent error (exit={result.returncode}): {snippet}"
            raise PipelineError(msg, code="FETCH_FAILED", retryable=False)
        if any(p in stderr_text for p in _NETWORK_ERROR_PATTERNS):
            snippet = result.stderr.decode("utf-8", errors="replace")[:500]
            msg = f"yt-dlp network error (exit={result.returncode}): {snippet}"
            raise PipelineError(msg, code="FETCH_FAILED", retryable=True)
        snippet = result.stderr.decode("utf-8", errors="replace")[:500]
        msg = f"yt-dlp exited {result.returncode}: {snippet}"
        raise PipelineError(msg, code="FETCH_FAILED", retryable=False)

    # yt-dlp wrote the file under {cache_dir}/{job_id}.{ext}; resolve it.
    candidates = sorted(
        p for p in cache_dir.glob(f"{job_id}.*")  # noqa: ASYNC240
        if p.is_file() and not p.name.endswith(".part")
    )
    if not candidates:
        msg = f"yt-dlp exited 0 but produced no file matching {cache_dir / job_id}.*"
        raise PipelineError(msg, code="FETCH_FAILED", retryable=False)
    return candidates[0]


# ---------- Stage 2: audio conditioning (ffmpeg) ----------


async def condition_audio(
    raw_path: Path,
    wav_path: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    proc_timeout_s: float = _CONDITION_TIMEOUT_S,
) -> Path:
    """Stage 2 — convert ``raw_path`` to 16 kHz mono PCM WAV at ``wav_path``.

    HLD-001 §11: normalises to 16 kHz mono PCM WAV (the format
    Whisper expects, so the gateway never has to re-encode). The
    raw input is **not** removed here — the orchestrator's
    ``try/finally`` handles deletion on both success and failure.

    The argv uses ``-ar 16000 -ac 1 -c:a pcm_s16le -f wav``; the
    verifier checks for exactly this shape.

    Errors (HLD-001 §12):

    - ``ffmpeg`` missing / non-zero / timeout → non-retryable
      ``AUDIO_CONDITIONING_FAILED``.
    """
    argv = [
        ffmpeg_bin,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(raw_path),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(wav_path),
    ]
    try:
        result = await _run_subprocess(argv, proc_timeout_s=proc_timeout_s)
    except FileNotFoundError as exc:
        msg = f"ffmpeg binary not found on PATH (looked for {ffmpeg_bin!r})"
        raise PipelineError(
            msg, code="AUDIO_CONDITIONING_FAILED", retryable=False
        ) from exc
    except TimeoutError as exc:
        msg = f"ffmpeg timed out after {proc_timeout_s}s converting {raw_path.name}"
        raise PipelineError(
            msg, code="AUDIO_CONDITIONING_FAILED", retryable=False
        ) from exc

    if result.returncode != 0:
        stderr_text = result.stderr.decode("utf-8", errors="replace")[:500]
        msg = f"ffmpeg exited {result.returncode}: {stderr_text}"
        raise PipelineError(msg, code="AUDIO_CONDITIONING_FAILED", retryable=False)

    if not wav_path.is_file():  # noqa: ASYNC240
        msg = f"ffmpeg exited 0 but {wav_path} was not created"
        raise PipelineError(msg, code="AUDIO_CONDITIONING_FAILED", retryable=False)
    return wav_path


# ---------- cleanup helper ----------


def cleanup_job_files(cache_dir: Path, job_id: str) -> int:
    """Remove every file in ``cache_dir`` whose name starts with ``{job_id}.``.

    Called from the orchestrator's ``try/finally`` so temp files are
    gone on both success and failure paths. Synchronous (just
    :meth:`Path.unlink`); fast enough that wrapping it in
    ``asyncio.to_thread`` would be overkill for the few files a
    single job produces.

    Returns the number of files actually removed (0 if the cache
    directory does not exist — which is the case when Stage 1 fails
    before creating it).
    """
    if not cache_dir.exists():
        return 0
    removed = 0
    for path in cache_dir.iterdir():  # noqa: ASYNC240
        if path.is_file() and path.name.startswith(f"{job_id}."):  # noqa: ASYNC240
            try:
                path.unlink()  # noqa: ASYNC240
                removed += 1
            except OSError as exc:
                # Best-effort cleanup; don't let a stale file
                # mask the original pipeline failure.
                logger.warning("cleanup: failed to remove %s: %s", path, exc)
    return removed


# ---------- Stage 3 engine contract ----------

# ``STTEngine`` is imported from ``local_transcription_service.stt.base``
# (canonical Protocol owned by the ``b2-stt-engine`` task). Anything
# that implements ``transcribe(wav_path, *, language) -> str`` plus
# ``is_ready() -> bool`` is accepted — duck typing keeps the
# orchestrator decoupled from the concrete engine implementation.


# ---------- RealPipeline orchestrator ----------


class RealPipeline(TranscriptionPipeline):
    """Three-stage pipeline: ``yt-dlp`` → ``ffmpeg`` → ``STTEngine``.

    Holds an injected ``STTEngine`` (Stage 3) and an audio cache
    directory. The worker instantiates one with the engine selected
    by ``settings.stt_engine`` and passes it to ``Worker(...)``.

    ``transcribe(video_url, *, job_id)``:

    1. **Stage 1** — fetch raw media into
       ``{audio_cache_dir}/{job_id}.{ext}``.
    2. **Stage 2** — condition to ``{audio_cache_dir}/{job_id}.wav``.
    3. **Stage 3** — delegate to ``stt_engine.transcribe(wav_path)``.
    4. **finally** — remove every ``{audio_cache_dir}/{job_id}.*``
       file. Runs on both success and failure paths.
    """

    def __init__(
        self,
        stt_engine: STTEngine,
        audio_cache_dir: Path,
        *,
        ytdlp_bin: str = "yt-dlp",
        ffmpeg_bin: str = "ffmpeg",
    ) -> None:
        self._stt_engine = stt_engine
        self._audio_cache_dir = audio_cache_dir
        self._ytdlp_bin = ytdlp_bin
        self._ffmpeg_bin = ffmpeg_bin

    @property
    def engine(self) -> STTEngine:
        """The Stage 3 STT engine this pipeline delegates to.

        Exposed publicly so the ``/ready`` probe can call
        ``engine.is_ready()`` (HLD-001 §8) without reaching into
        private state. The engine is the source of truth for
        "model loaded" — adding a new ``STTEngine`` implementation
        (e.g. ``mlx-whisper`` later) doesn't require touching
        ``api/health.py``.
        """
        return self._stt_engine

    async def transcribe(self, video_url: str, *, job_id: str) -> str:
        """Run Stage 1 → Stage 2 → Stage 3 and clean up on every path."""
        cache_dir = self._audio_cache_dir
        wav_path = cache_dir / f"{job_id}.wav"
        try:
            raw_path = await fetch_media(
                cache_dir, video_url, job_id, ytdlp_bin=self._ytdlp_bin
            )
            await condition_audio(raw_path, wav_path, ffmpeg_bin=self._ffmpeg_bin)
            return await self._stt_engine.transcribe(wav_path)
        finally:
            cleanup_job_files(cache_dir, job_id)


__all__ = [
    "RealPipeline",
    "STTEngine",
    "cleanup_job_files",
    "condition_audio",
    "fetch_media",
]