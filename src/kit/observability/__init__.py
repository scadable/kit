"""Logging and telemetry.

JSON to stdout, always, with ``trace_id`` and ``span_id`` injected into every
line from the active span. OpenTelemetry over OTLP, push only.

Metrics are OTLP push too. There is no ``/metrics`` endpoint and no Prometheus
dependency anywhere in the fleet: services push to one Collector, which is one
scrape target for Prometheus instead of seven services times N replicas, and
nothing has to discover pods to find metrics. Prometheus and Grafana still work
exactly as expected; they just read from the Collector.

Trace context propagates whether or not anything is exported, in both
directions, so a request keeps one identity across services and every log line
carries its ``trace_id`` even with no collector configured.

Telemetry is an optional extra. Install ``scadable-kit[otel]`` to export; without
it a service still logs JSON, it simply logs without trace correlation.
"""

from kit.observability._logging import JSONFormatter, configure_logging, trace_context
from kit.observability._metrics import (
    CLIENT_ATTEMPTS,
    CLIENT_BREAKER_OPEN,
    CLIENT_DURATION,
    CLIENT_REQUESTS,
    SERVER_DURATION,
    SERVER_REQUESTS,
    record,
    shutdown_metrics,
    start_metrics,
)
from kit.observability._tracing import (
    instrument_app,
    instrument_client,
    shutdown_telemetry,
    start_telemetry,
)

__all__ = [
    "CLIENT_ATTEMPTS",
    "CLIENT_BREAKER_OPEN",
    "CLIENT_DURATION",
    "CLIENT_REQUESTS",
    "SERVER_DURATION",
    "SERVER_REQUESTS",
    "JSONFormatter",
    "configure_logging",
    "instrument_app",
    "instrument_client",
    "record",
    "shutdown_metrics",
    "shutdown_telemetry",
    "start_metrics",
    "start_telemetry",
    "trace_context",
]
