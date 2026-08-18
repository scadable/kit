"""OTLP tracing setup.

Explicit instrumentation, never ``opentelemetry-instrument`` auto-discovery.
Auto-instrumentation is runtime monkey-patching that decides for itself what to
wrap, and for this product the risk is specific: it will happily capture
repository paths, SQL parameters and arbitrary headers into spans, which is the
class of data we exist to keep track of.

THE PROVIDER IS INSTALLED WHETHER OR NOT ANYTHING IS EXPORTED. Only the exporter
is conditional on an endpoint. That is what makes the next sentence true, and it
was not true before: a service with no collector configured still propagates
trace context and still puts ``trace_id`` on every log line, so one identifier
follows a request across services in an environment that has no tracing backend
at all. Grepping one id across five services is most of the value here, and it
should not require infrastructure.

It is not free. A non-exporting service still creates spans; with no span
processor attached they are dropped immediately, which is cheap rather than
costless. The trade buys log correlation everywhere, and a correlation id that
is missing in development is a correlation id nobody trusts in production.
"""

from __future__ import annotations

import logging
from typing import Any

EXPORT_TIMEOUT_SECONDS = 5
"""Inside the default 10s shutdown budget."""

_PROBE_PATHS = "/healthz,/readyz"
"""Excluded from tracing. Otherwise every probe becomes a sampled span and the
latency percentiles describe the kubelet rather than customers."""

_provider: Any = None

log = logging.getLogger("kit.observability")


def signal_endpoint(endpoint: str, signal: str) -> str:
    """Append the signal path unless the caller already gave one.

    Passing `endpoint=` to an OTLP exporter overrides the SDK entirely and is
    used VERBATIM. Only when the SDK reads OTEL_EXPORTER_OTLP_ENDPOINT itself
    does it append /v1/traces or /v1/metrics. So handing both exporters one base
    URL posts both signals to the collector's root, which answers 404, and the
    only symptom is an export error in the logs while the service serves
    perfectly. That is exactly how this shipped.

    Both forms are accepted because both exist in the wild: a base URL from an
    operator who read the OTel docs, and a full signal URL from one who read
    this kit's tests.
    """
    if not endpoint:
        return endpoint
    trimmed = endpoint.rstrip("/")
    if trimmed.endswith(("/v1/traces", "/v1/metrics", "/v1/logs")):
        return trimmed
    return f"{trimmed}/v1/{signal}"


def start_telemetry(
    *,
    service_name: str,
    version: str,
    environment: str,
    endpoint: str = "",
) -> bool:
    """Install the tracer provider. Returns whether telemetry is INSTALLED.

    Not whether it is exporting, and the distinction is the whole fix. The
    caller uses this answer to decide whether to instrument, and instrumentation
    is what propagates context and correlates logs. Gating it on the endpoint,
    which is what this used to do, left a service without a collector with no
    trace ids anywhere, while this module's own docstring promised otherwise.

    Everything except the endpoint is read by the OpenTelemetry SDK from its own
    standard ``OTEL_*`` variables, so sampling, propagators and headers are
    configured the way every other OTel deployment configures them rather than
    through a second set of names that can disagree.
    """
    global _provider

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError:
        if endpoint:
            # Configured to export, but the extra is not installed. Loud,
            # because the operator asked for something they are not getting and
            # continuing silently means discovering it when a trace is needed,
            # which is during an incident.
            log.warning(
                "telemetry endpoint is configured but the otel extra is not installed",
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
    provider = TracerProvider(resource=resource)

    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Bounded, for the same reason as metrics: shutdown flushes, and an
        # unreachable collector must not outlast the pod's termination grace
        # period and turn a clean stop into a SIGKILL.
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=signal_endpoint(endpoint, "traces"), timeout=EXPORT_TIMEOUT_SECONDS
                )
            )
        )

    trace.set_tracer_provider(provider)
    _provider = provider
    return True


def instrument_app(app: Any) -> None:
    """Instrument FastAPI once.

    Once, not twice: instrumenting FastAPI and the generic ASGI layer both
    produces two spans per request and doubles every latency percentile.

    This is where INBOUND propagation comes from. A request carrying
    ``traceparent`` joins that trace; one without starts a new one. Both halves
    are the instrumentation's doing rather than ours.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return

    FastAPIInstrumentor.instrument_app(app, excluded_urls=_PROBE_PATHS)


def instrument_client(client: Any) -> None:
    """Instrument ONE httpx client, so outbound calls carry ``traceparent``.

    The other half of propagation, and the half that was missing entirely:
    without it a call from service A to service B sends no context, B starts a
    brand new trace, and one request appears in the backend as two unrelated
    traces with nothing linking them.

    Per client rather than process-wide. ``HTTPXClientInstrumentor().instrument()``
    patches httpx globally, which is the auto-discovery behaviour this module
    exists to avoid: it would also wrap the test suite's own clients and
    anything a dependency happens to construct.
    """
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:
        return

    HTTPXClientInstrumentor.instrument_client(client)


def shutdown_telemetry() -> None:
    """Flush pending spans on the way down.

    Without this a container that exits promptly loses the spans describing
    whatever made it exit, which are the ones worth having.
    """
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None
