"""One request, one trace, across two services.

This is the behaviour the fleet was assumed to have and did not. Before this
change `kit.observability` instrumented FastAPI and nothing else, so a call from
service A to service B carried no `traceparent`, B started a BRAND NEW trace, and
one request appeared in the backend as two unrelated traces with nothing linking
them. Nothing failed and nothing logged a warning.

The second half was worse in a quieter way: with no OTLP endpoint configured the
tracer provider was never installed at all, so `trace_id` was absent from every
log line, while the module's own docstring said context still propagates.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from kit.clients import Upstream, service_client
from kit.health import Registry
from kit.httpapi import install_conventions
from kit.observability import (
    JSONFormatter,
    instrument_app,
    shutdown_telemetry,
    start_telemetry,
)

pytest.importorskip("opentelemetry.sdk", reason="needs the otel extra")


@pytest.fixture(autouse=True)
def telemetry() -> Any:
    """No endpoint, deliberately. That is the case that was broken."""
    start_telemetry(service_name="a", version="test", environment="test")
    yield
    shutdown_telemetry()


def upstream_app() -> tuple[FastAPI, dict[str, str]]:
    """Service B. Records the trace context it was reached with."""
    seen: dict[str, str] = {}
    app = FastAPI()
    install_conventions(app, readiness=Registry())

    @app.get("/downstream")
    async def downstream(request: Request) -> dict[str, bool]:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        seen["traceparent"] = request.headers.get("traceparent", "")
        seen["trace_id"] = format(context.trace_id, "032x") if context.is_valid else ""
        seen["request_id"] = request.headers.get("X-Request-ID", "")
        return {"ok": True}

    instrument_app(app)
    return app, seen


async def test_a_call_between_services_is_one_trace() -> None:
    """THE test. Without outbound instrumentation B starts its own trace and
    the two halves of one request cannot be joined in any backend."""
    downstream, seen = upstream_app()

    caller = FastAPI()
    install_conventions(caller, readiness=Registry())
    client = service_client(
        Upstream(name="downstream", base_url="http://downstream"),
        transport=httpx.ASGITransport(app=downstream),
    )

    origin: dict[str, str] = {}

    @caller.get("/call")
    async def call() -> dict[str, bool]:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        origin["trace_id"] = format(context.trace_id, "032x") if context.is_valid else ""
        await client.get_json("/downstream")
        return {"ok": True}

    instrument_app(caller)

    transport = httpx.ASGITransport(app=caller)
    async with httpx.AsyncClient(transport=transport, base_url="http://caller") as opened:
        assert (await opened.get("/call")).status_code == 200
    await client.aclose()

    assert seen["traceparent"], "the outbound call carried no trace context at all"
    assert origin["trace_id"], "the caller had no trace to propagate"
    assert seen["trace_id"] == origin["trace_id"], (
        "the downstream service started its own trace; one request is two traces"
    )


async def test_an_inbound_traceparent_is_joined_rather_than_replaced() -> None:
    """The first service in a chain assigns the id; every later one continues
    it. A service that replaced it would break the chain at itself."""
    app, seen = upstream_app()

    incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://b") as client:
        await client.get("/downstream", headers={"traceparent": incoming})

    assert seen["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


async def test_a_request_without_one_starts_a_trace() -> None:
    app, seen = upstream_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://b") as client:
        await client.get("/downstream")

    assert seen["trace_id"], "no trace was started for an uninstrumented caller"
    assert seen["trace_id"] != "0" * 32


async def test_the_request_id_crosses_the_boundary_unchanged() -> None:
    """A fresh id per hop would make each service's logs internally consistent
    and unjoinable to each other, which is the opposite of the point."""
    downstream, seen = upstream_app()

    caller = FastAPI()
    install_conventions(caller, readiness=Registry())
    client = service_client(
        Upstream(name="downstream", base_url="http://downstream"),
        transport=httpx.ASGITransport(app=downstream),
    )

    @caller.get("/call")
    async def call() -> dict[str, bool]:
        await client.get_json("/downstream")
        return {"ok": True}

    transport = httpx.ASGITransport(app=caller)
    async with httpx.AsyncClient(transport=transport, base_url="http://caller") as opened:
        response = await opened.get("/call", headers={"X-Request-ID": "known-id"})
    await client.aclose()

    assert response.headers["X-Request-ID"] == "known-id"
    assert seen["request_id"] == "known-id"


async def test_logs_carry_a_trace_id_with_no_collector_configured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The second bug. `start_telemetry` used to return before installing a
    provider when no endpoint was set, so `_logging` found no valid span context
    and every line went out with no correlation id, in exactly the environments
    where somebody is reading the logs by hand.
    """
    app, _ = upstream_app()

    transport = httpx.ASGITransport(app=app)
    with caplog.at_level(logging.INFO, logger="kit.httpapi"):
        async with httpx.AsyncClient(transport=transport, base_url="http://b") as client:
            await client.get("/downstream")

    lines = [JSONFormatter().format(record) for record in caplog.records]
    assert lines, "no request was logged"
    assert any('"trace_id"' in line for line in lines), (
        "no log line carried a trace id, with the provider installed"
    )


def test_instrumenting_a_client_without_the_extra_is_a_no_op(
    monkeypatch: pytest.LogCaptureFixture,
) -> None:
    """A service that does not export traces still builds clients. Failing here
    would make telemetry a hard dependency of calling anything."""
    import builtins

    from kit.observability import instrument_client as instrument

    real = builtins.__import__

    def missing(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("opentelemetry.instrumentation.httpx"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)  # type: ignore[attr-defined]

    instrument(httpx.AsyncClient())


def test_no_extra_and_no_endpoint_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing was asked for and nothing is installed, so there is nothing to
    warn about. The warning is reserved for an operator who configured an
    endpoint and is not getting it."""
    import builtins

    real = builtins.__import__

    def missing(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("opentelemetry"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)

    assert start_telemetry(service_name="s", version="v", environment="test") is False


# --- the endpoint bug that shipped in 0.5.0 ---------------------------------


@pytest.mark.parametrize(
    ("given", "signal", "expected"),
    [
        ("http://collector:4318", "traces", "http://collector:4318/v1/traces"),
        ("http://collector:4318", "metrics", "http://collector:4318/v1/metrics"),
        ("http://collector:4318/", "traces", "http://collector:4318/v1/traces"),
        # Already specific: left alone rather than doubled.
        ("http://c:4318/v1/traces", "traces", "http://c:4318/v1/traces"),
        ("", "traces", ""),
    ],
)
def test_the_endpoint_gets_its_signal_path(given: str, signal: str, expected: str) -> None:
    """Passing `endpoint=` to an OTLP exporter overrides the SDK and is used
    VERBATIM; only reading OTEL_EXPORTER_OTLP_ENDPOINT itself makes the SDK
    append the signal path.

    0.5.0 handed both exporters one base URL, so traces and metrics both POSTed
    to the collector's root and got 404. The service served perfectly and the
    only evidence was an export error in the logs, which is why this reached a
    cluster before anyone noticed.
    """
    from kit.observability._tracing import signal_endpoint

    assert signal_endpoint(given, signal) == expected


def test_traces_and_metrics_do_not_share_one_url() -> None:
    """The specific thing that was broken: one endpoint cannot be both signals."""
    from kit.observability._tracing import signal_endpoint

    base = "http://collector:4318"

    assert signal_endpoint(base, "traces") != signal_endpoint(base, "metrics")
