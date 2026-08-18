"""OTLP metrics. Push only, and there is no ``/metrics`` endpoint anywhere.

The reason is not the one the fleet used to give. That argument was about App
Platform putting replicas behind a load balancer, so a scrape reached one
replica at random; it died when everything moved into the cluster. The reason
that survives is simpler: services push to one Collector, which is one scrape
target for Prometheus instead of seven services times N replicas, and nothing
has to discover pods to find metrics. A service that moves does not change how
it is monitored.

CARDINALITY IS THE FAILURE MODE, not throughput. Every label value multiplies
the series count, so one label carrying a tenant id, a raw path or an exact URL
turns one series into millions and takes Prometheus down. Recovering from that
means deleting data, not changing a setting. Every instrument below takes a
bounded set: route PATTERNS, upstream names, methods, status codes. There is a
test asserting no instrument accepts free-form text.
"""

from __future__ import annotations

import logging
from typing import Any

from kit.observability._tracing import signal_endpoint

_meter: Any = None
_provider: Any = None
_instruments: dict[str, Any] = {}

log = logging.getLogger("kit.observability")

EXPORT_TIMEOUT_SECONDS = 5
"""Comfortably inside the default 10s shutdown budget, so a collector that is
down delays termination rather than preventing it."""

SERVER_REQUESTS = "http.server.requests"
SERVER_DURATION = "http.server.duration"
CLIENT_REQUESTS = "http.client.requests"
CLIENT_DURATION = "http.client.duration"
CLIENT_ATTEMPTS = "http.client.attempts"
CLIENT_BREAKER_OPEN = "http.client.breaker_open"


def start_metrics(
    *,
    service_name: str,
    version: str,
    environment: str,
    endpoint: str = "",
) -> bool:
    """Install the meter provider. Returns whether metrics are being exported.

    Unlike tracing, this DOES return early without an endpoint, and the
    asymmetry is deliberate. A tracer with no exporter still earns its keep by
    propagating context and correlating logs. A meter with no exporter earns
    nothing: it accumulates numbers in memory that no one will ever read, and
    the histograms are the expensive part.
    """
    global _meter, _provider, _instruments

    if not endpoint:
        return False

    try:
        from opentelemetry import metrics
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        log.warning(
            "metrics endpoint is configured but the otel extra is not installed",
            extra={"endpoint_configured": True},
        )
        return False

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": version,
            "deployment.environment.name": environment,
        }
    )
    # A BOUNDED timeout, and it matters on the way down rather than in steady
    # state. Shutdown force-flushes, and against an unreachable collector the
    # exporter's default retry can outlast the pod's graceful termination
    # period, at which point Kubernetes SIGKILLs a process that was trying to
    # report why it stopped. Telemetry must never be the reason a shutdown is
    # not clean.
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(
            endpoint=signal_endpoint(endpoint, "metrics"), timeout=EXPORT_TIMEOUT_SECONDS
        )
    )
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)

    _provider = provider
    _meter = provider.get_meter("kit")
    _instruments = _build(_meter)
    return True


def _build(meter: Any) -> dict[str, Any]:
    """Every instrument, created once.

    Once, because creating an instrument per call leaks memory in the SDK and
    silently produces duplicate series that a dashboard then sums twice.
    """
    return {
        SERVER_REQUESTS: meter.create_counter(
            SERVER_REQUESTS, unit="1", description="Requests served, by route pattern"
        ),
        SERVER_DURATION: meter.create_histogram(
            SERVER_DURATION, unit="ms", description="Time to serve a request"
        ),
        CLIENT_REQUESTS: meter.create_counter(
            CLIENT_REQUESTS, unit="1", description="Outbound calls, by upstream and outcome"
        ),
        CLIENT_DURATION: meter.create_histogram(
            CLIENT_DURATION, unit="ms", description="Time for an outbound call, retries included"
        ),
        CLIENT_ATTEMPTS: meter.create_histogram(
            CLIENT_ATTEMPTS,
            unit="1",
            description="Attempts per outbound call. Above 1 is retry amplification.",
        ),
        CLIENT_BREAKER_OPEN: meter.create_up_down_counter(
            CLIENT_BREAKER_OPEN, unit="1", description="Upstreams currently being shed"
        ),
    }


def record(name: str, value: float, **labels: str | int) -> None:
    """Add to an instrument, or do nothing when metrics are not installed.

    Doing nothing is the common case in development and in tests, and it has to
    be silent: a metric call that raises when unconfigured would put telemetry
    in the request path's failure modes, which is exactly backwards.

    Labels are keyword-only and every call site in the kit passes bounded
    values. See the module docstring on why that is not a style preference.
    """
    instrument = _instruments.get(name)
    if instrument is None:
        return

    attributes = {key: value for key, value in labels.items()}
    if hasattr(instrument, "record"):
        instrument.record(value, attributes)
    else:
        instrument.add(value, attributes)


def shutdown_metrics() -> None:
    """Flush pending metrics on the way down.

    The periodic reader exports on an interval, so without this the final
    interval is lost. That interval is the one covering whatever made the
    process exit.
    """
    global _meter, _provider, _instruments
    if _provider is not None:
        _provider.shutdown()
    _provider = None
    _meter = None
    _instruments = {}
