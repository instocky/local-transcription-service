"""In-process metrics primitives (HLD-001 §15.1, Phase D, 2026-07-04).

The service's HLD explicitly says "no metrics endpoint for MVP"
(§15) — observability surfaces as JSON log lines instead of a
Prometheus endpoint. This module is the structured-log counterpart:
a tiny counter that emits an ``error_rate_tick`` event every N
seconds with per-code counts since the last tick.

Why a counter (not a Prometheus client):

- HLD §15 promise kept: no new HTTP endpoint, no extra port.
- A single-user tool on a LAN has no metrics aggregation infra;
  the operator's existing log-tailing workflow is the dashboard.
- If a future phase wants Prometheus, this counter is the right
  place to extend — the metric names already line up with the
  log event names, so a Prometheus exporter is a 30-line wrapper.

Threading: asyncio is single-threaded but ``asyncio.gather`` runs
tasks cooperatively. ``ErrorRateCounter`` uses an ``asyncio.Lock``
not for serialisation (it's already single-threaded) but as a
documentation marker — the increment is dict-update + return,
which is atomic in CPython.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ErrorRateCounter:
    """Per-code failure counter with a 60-second tick.

    Public methods:

    - :meth:`increment` — add 1 to a code's bucket. Called from
      :class:`~local_transcription_service.worker.Worker` on every
      terminal FAIL (NOT on retryable defer — only on the path
      that reaches ``mark_failed``).
    - :meth:`tick` — return the current counts AND reset to zero
      in one operation. Called from the periodic background task.
    - :meth:`reset` — clear without returning. Test helper.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def increment(self, code: str) -> None:
        """Add 1 to the count for ``code``.

        Synchronous — CPython's GIL makes ``dict[k] = dict.get(k, 0) + 1``
        atomic at the interpreter level; no real lock needed. The
        ``asyncio.Lock`` is held but never contended; it's a
        documentation marker for "this is async-safe by construction".
        """
        self._counts[code] = self._counts.get(code, 0) + 1

    def tick(self) -> dict[str, int]:
        """Return current counts and reset to zero.

        Returns a fresh dict — callers may mutate it freely without
        affecting the counter's internal state.
        """
        snapshot = dict(self._counts)
        self._counts.clear()
        return snapshot

    def reset(self) -> None:
        """Clear all counts. Returns nothing.

        Distinct from :meth:`tick` because tick returns the snapshot;
        reset is for tests that want to start from zero without
        inspecting the state.
        """
        self._counts.clear()


async def run_error_rate_loop(
    counter: ErrorRateCounter,
    *,
    interval_seconds: int = 60,
    stop_event: asyncio.Event | None = None,
    log: logging.Logger | None = None,
) -> None:
    """Emit one ``error_rate_tick`` log line every ``interval_seconds``.

    Args:
        counter: the counter to drain.
        interval_seconds: how often to emit. Default 60 s.
        stop_event: optional event to break out of the loop
            cleanly. The worker passes ``self._stop`` here so
            graceful shutdown waits for the in-flight tick to
            complete.
        log: optional logger. Defaults to this module's logger.

    Cancellation: the loop awaits ``stop_event.wait()`` with the
    interval as a timeout, so SIGINT during a quiet tick returns
    immediately. A SIGINT during the log emit itself propagates as
    a CancelledError — the worker catches it via the surrounding
    ``asyncio.gather(..., return_exceptions=True)``.
    """
    logger_ = log or logging.getLogger(__name__)
    event = stop_event or asyncio.Event()

    while not event.is_set():
        # Wait for either the stop signal or the interval timeout.
        try:
            await asyncio.wait_for(event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
        else:
            # Stop event fired — exit cleanly without emitting a
            # final tick (operators reading the log post-shutdown
            # don't want a half-empty interval).
            return

        counts = counter.tick()
        logger_.info(
            "error_rate_tick",
            extra={
                "event": "error_rate_tick",
                "interval_s": interval_seconds,
                "counts": counts,
            },
        )


__all__ = ["ErrorRateCounter", "run_error_rate_loop"]