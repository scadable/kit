"""OTLP tracing setup.

Explicit instrumentation, never ``opentelemetry-instrument`` auto-discovery.
Auto-instrumentation is runtime monkey-patching that decides for itself what to
wrap, and for this product the risk is specific: it will happily capture
repository paths, SQL parameters and arbitrary headers into spans, which is the
class of data we exist to keep track of.

No exporter endpoint configured means no exporter is installed. The service still
logs JSON, and trace context still propagates, so a request crossing services
keeps its identity even where nothing is being exported.
"""

from __future__ import annotations

import logging
from typing import Any

_PROBE_PATHS = "/healthz,/readyz"
"""Excluded from tracing. Otherwise every probe becomes a sampled span and the
latency percentiles describe the kubelet rather than customers."""

_provider: Any = None

log = logging.getLogger("kit.observability")


def start_telemetry(
    *,
    service_name: str,
    version: str,
    environment: str,
    endpoint: str = "",
) -> bool:
    """Install the tracer provider. Returns whether exporting is on.

    Everything except the endpoint is read by the OpenTelemetry SDK from its own
    standard ``OTEL_*`` variables, so sampling and headers are configured the way
    every other OTel deployment configures them rather than through a second set
    of names that can disagree.
    """
    global _provider

    if not endpoint:
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        # Configured to export, but the extra is not installed. Loud, because
        # the operator asked for something they are not getting, and continuing
        # silently means discovering it when a trace is needed.
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
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _provider = provider
    return True


def instrument_app(app: Any) -> None:
    """Instrument FastAPI once.

    Once, not twice: instrumenting FastAPI and the generic ASGI layer both
    produces two spans per request and doubles every latency percentile.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return

    FastAPIInstrumentor.instrument_app(app, excluded_urls=_PROBE_PATHS)


def shutdown_telemetry() -> None:
    """Flush pending spans on the way down.

    Without this a container that exits promptly loses the spans describing
    whatever made it exit, which are the ones worth having.
    """
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None
