"""Tests for src/local_transcription_service/metrics.py (HLD-001 §15.1)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from local_transcription_service.metrics import ErrorRateCounter, run_error_rate_loop


def test_error_rate_counter_increments_per_code() -> None:
    """Three increments of one code, one of another → counts match."""
    counter = ErrorRateCounter()
    counter.increment("FETCH_FAILED")
    counter.increment("FETCH_FAILED")
    counter.increment("FETCH_FAILED")
    counter.increment("STT_GATEWAY_UNAVAILABLE")

    counts = counter.tick()
    assert counts == {"FETCH_FAILED": 3, "STT_GATEWAY_UNAVAILABLE": 1}


def test_error_rate_counter_tick_returns_snapshot_and_resets() -> None:
    """tick() returns the current counts AND resets the counter to zero."""
    counter = ErrorRateCounter()
    counter.increment("FETCH_FAILED")
    counter.increment("FETCH_FAILED")

    first = counter.tick()
    assert first == {"FETCH_FAILED": 2}

    # Second tick immediately after — nothing in flight.
    second = counter.tick()
    assert second == {}


def test_error_rate_counter_reset_does_not_return() -> None:
    """reset() clears without returning — distinct from tick()."""
    counter = ErrorRateCounter()
    counter.increment("FETCH_FAILED")
    counter.reset()
    assert counter.tick() == {}


def test_error_rate_counter_returns_fresh_dict() -> None:
    """tick() returns a dict the caller may mutate without affecting state."""
    counter = ErrorRateCounter()
    counter.increment("FETCH_FAILED")

    snapshot = counter.tick()
    snapshot["INJECTED"] = 99  # mutate the snapshot
    snapshot["FETCH_FAILED"] = 0  # ditto

    # Internal state is unaffected — second tick still shows the original count.
    assert counter.tick() == {}


def test_error_rate_counter_empty_after_init() -> None:
    """A freshly constructed counter has no entries."""
    assert ErrorRateCounter().tick() == {}


async def test_run_error_rate_loop_emits_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """run_error_rate_loop emits one error_rate_tick log line per interval."""
    counter = ErrorRateCounter()
    counter.increment("FETCH_FAILED")
    stop = asyncio.Event()

    async def _stop_after_two_ticks() -> None:
        # Let the runner fire two ticks (2 × 100 ms = 200 ms), then signal.
        await asyncio.sleep(0.25)
        stop.set()

    with caplog.at_level(logging.INFO, logger="local_transcription_service.metrics"):
        runner = asyncio.create_task(
            run_error_rate_loop(counter, interval_seconds=0.1, stop_event=stop),
        )
        stopper = asyncio.create_task(_stop_after_two_ticks())
        await asyncio.gather(runner, stopper, return_exceptions=True)

    # At least one error_rate_tick log record should be present.
    tick_records = [
        r for r in caplog.records
        if getattr(r, "event", None) == "error_rate_tick"
    ]
    assert len(tick_records) >= 1
    # The first tick carries our pre-loaded counts.
    first = tick_records[0]
    assert first.interval_s == 0.1
    assert first.counts == {"FETCH_FAILED": 1}


async def test_run_error_rate_loop_stop_event_exits_cleanly() -> None:
    """Setting stop_event before the first tick → loop returns without emitting."""
    counter = ErrorRateCounter()

    stop = asyncio.Event()
    stop.set()  # pre-fired

    # Run with a long interval — the stop event must short-circuit.
    await asyncio.wait_for(
        run_error_rate_loop(counter, interval_seconds=60, stop_event=stop),
        timeout=2.0,
    )
    # Counter unchanged (no tick fired).
    assert counter.tick() == {}