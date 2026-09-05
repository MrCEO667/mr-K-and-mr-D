"""Structured logging with a run_id threaded through every record.

Every write in this system belongs to a run. A log line that cannot be tied
back to one is not much use when a collector quietly degrades at 3am, so
run_id is bound into the logger rather than passed at each call site.
"""
from __future__ import annotations

import json
import logging
import sys

_CONFIGURED = False

# Anything the LogRecord carries by default; everything else a caller attaches
# is treated as structured context and serialised into the line.
_STANDARD = frozenset(
    [
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    ]
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Greppable by run_id, parseable by anything."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup(level: str = "INFO", *, json_output: bool = True) -> None:
    """Install the root handler. Idempotent -- entry points may both call it."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    _CONFIGURED = True


class _ContextAdapter(logging.LoggerAdapter):
    """Merges bound context with per-call extra.

    logging.LoggerAdapter.process replaces kwargs["extra"] outright, so a
    bound run_id would silently delete the fields passed at the call site --
    the logs would look fine and carry none of the detail.
    """

    def process(self, msg, kwargs):
        merged = dict(self.extra or {})
        merged.update(kwargs.get("extra") or {})
        kwargs["extra"] = merged
        return msg, kwargs


def get(name: str, **context: object) -> logging.LoggerAdapter:
    """A logger with permanent structured context, normally run_id=...

    The adapter merges its context into every record, so a caller cannot
    forget to include the run_id on the one line that later matters.
    """
    return _ContextAdapter(logging.getLogger(name), dict(context))


def reset_for_tests() -> None:
    global _CONFIGURED
    _CONFIGURED = False
