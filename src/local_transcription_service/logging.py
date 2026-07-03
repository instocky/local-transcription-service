"""Structured JSON logging per HLD-001 §15.

HLD-001 mandates one JSON line per event on stdout (launchd captures
the stream to ``~/Library/Logs/local-transcription-service.log``). The
fields HLD names explicitly are ``ts`` (ISO 8601 UTC), ``level``,
``event``, plus any task-specific extras (``job_id``, ``stage``,
``duration_s``, ``stt_engine`` ...).

This module exposes a :class:`logging.Formatter` that emits JSON with
those fields, and a :func:`configure_logging` helper that installs a
stdout handler on the root logger so callers don't have to wire up
the formatter by hand. Callers pass structured fields via the
``extra`` argument:

.. code-block:: python

    logger.info("stage finished", extra={
        "event": "stage_finished",
        "stage": "fetch",
        "job_id": job_id,
        "duration_s": elapsed,
    })

Stdlib logging already provides enough hooks to ship JSON without a
new dependency (e.g. ``python-json-logger``), keeping the lockfile
flat and the surface area small.

Reserved ``LogRecord`` attributes (``name``, ``msg``, ``args``,
``levelname``, ``levelno``, ``pathname``, ``filename``, ``module``,
``exc_info``, ``exc_text``, ``stack_info``, ``lineno``, ``funcName``,
``created``, ``msecs``, ``relativeCreated``, ``thread``,
``threadName``, ``processName``, ``process``, ``taskName``) are
filtered out of the emitted payload; only the operator-facing fields
remain.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

#: ``LogRecord`` attributes that stdlib already populates and we never
#: want to surface to the operator. Anything not in this set and not
#: a built-in is treated as a structured field passed via ``extra=``.
_RESERVED_LOG_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Emit each :class:`~logging.LogRecord` as a single JSON line.

    The payload always includes ``ts`` (ISO 8601 UTC), ``level``, and
    ``logger``. Structured fields passed via ``extra=`` are merged in
    verbatim. The formatted ``message`` is preserved under the
    ``message`` key for human readability.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_RECORD_KEYS or key.startswith("_"):
                continue
            if key in payload:
                # Caller asked for ``event``/``level``/etc. via extra;
                # their explicit value wins over the default.
                payload[key] = value
            else:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # ``sort_keys=False`` keeps the HLD-canonical field order
        # (ts, level, ...). JSON Lines spec allows any order, but a
        # stable shape makes the log file easier to grep.
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install a stdout JSON handler on the root logger.

    Idempotent: replaces any handlers previously installed by this
    function so repeated calls (e.g. test fixtures + app startup) do
    not stack duplicate handlers. Other loggers configured by
    libraries propagate to the root and pick up the JSON formatter.

    `level` is a stdlib log-level name (``"DEBUG"``, ``"INFO"``,
    ``"WARNING"``, ...). Defaults to ``INFO`` to match HLD-001's
    "one log line per event" intent — debug-level noise is only
    useful in development.
    """
    root = logging.getLogger()
    # Drop our previously-installed handler so re-entry doesn't stack.
    for h in list(root.handlers):
        if getattr(h, "_lts_json_handler", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._lts_json_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


__all__ = ["JsonFormatter", "configure_logging"]