"""JSON logs on stdout, correlated with the active trace."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

# Attributes the stdlib puts on every record. Anything else a caller passed via
# `extra` is ours and belongs in the line.
_STANDARD = frozenset(
    """
    args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName
    relativeCreated stack_info thread threadName taskName
    """.split()
)

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def trace_context() -> dict[str, str]:
    """The active trace and span, when there is one.

    Imported lazily so the kit works without the telemetry extra installed: a
    service that exports nothing still logs, it just logs without correlation.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return {}

    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return {}
    return {
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
    }


class JSONFormatter(logging.Formatter):
    """One JSON object per line.

    ``trace_id`` and ``span_id`` are injected on EVERY line rather than by the
    caller, because a correlation that depends on remembering is a correlation
    that is missing from the line you need.
    """

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                line[key] = value

        if record.exc_info:
            # The type and message, never the traceback: a traceback in a JSON
            # field is unreadable, and the same exception is already reported to
            # the error tracker with its frames intact.
            exc_type, exc_value, _ = record.exc_info
            if exc_type is not None:
                line["error_type"] = exc_type.__name__
                line["error"] = str(exc_value)

        line.update(trace_context())
        return json.dumps(line, default=str)


def configure_logging(level: str = "info", stream: Any = None) -> None:
    """Send JSON to stdout and nothing anywhere else.

    Stdout because the container runtime collects it, and one object per line
    because that is what a log pipeline can parse without a custom rule.

    Called once at startup. Without a configured root handler the standard
    library drops INFO through its last-resort handler, which is how a service's
    own startup lines become invisible in production.
    """
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(_LEVELS.get(level.lower(), logging.INFO))

    # Uvicorn installs its own handlers and its access logger prints the URL,
    # which carries ids and tokens. The kit's request logger records the route
    # pattern instead, so the access log is silenced rather than duplicated.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
