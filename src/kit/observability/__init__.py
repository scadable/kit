"""Logging and telemetry.

JSON to stdout, always, with ``trace_id`` and ``span_id`` injected into every
line from the active span. OpenTelemetry over OTLP, push only.

There is no ``/metrics`` endpoint and no Prometheus dependency anywhere in the
fleet. Replicas have separate filesystems, so a load-balanced scrape reaches
exactly one of them and multiprocess collection writes state to a disk that is
wiped on replacement.

Telemetry is an optional extra. Install ``scadable-kit[otel]`` to export; without
it a service still logs JSON, it simply logs without trace correlation.
"""

from kit.observability._logging import JSONFormatter, configure_logging
from kit.observability._tracing import (
    instrument_app,
    shutdown_telemetry,
    start_telemetry,
)

__all__ = [
    "JSONFormatter",
    "configure_logging",
    "instrument_app",
    "shutdown_telemetry",
    "start_telemetry",
]
