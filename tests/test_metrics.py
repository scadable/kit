"""Metrics, and the label that takes a metrics backend down.

Cardinality is the failure mode here, not throughput. Every distinct label value
is another time series, so ONE label carrying a path with an id in it turns one
series into one per id, and recovering means deleting data rather than changing
a setting.

The instruments are replaced with recorders below, which is what lets a test
assert on the labels the kit actually passes rather than on the ones it meant to.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from kit.health import Registry
from kit.httpapi import install_conventions
from kit.observability import _metrics
from kit.observability._metrics import (
    CLIENT_ATTEMPTS,
    CLIENT_REQUESTS,
    SERVER_DURATION,
    SERVER_REQUESTS,
    record,
    shutdown_metrics,
    start_metrics,
)


class Recorder:
    """Stands in for an instrument and keeps what it was given."""

    def __init__(self) -> None:
        self.points: list[tuple[float, dict[str, Any]]] = []

    def add(self, value: float, attributes: dict[str, Any]) -> None:
        self.points.append((value, attributes))

    def labels(self, key: str) -> list[Any]:
        return [attributes.get(key) for _value, attributes in self.points]


@pytest.fixture
def recorders(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recorder]:
    instruments = {
        name: Recorder()
        for name in (SERVER_REQUESTS, SERVER_DURATION, CLIENT_REQUESTS, CLIENT_ATTEMPTS)
    }
    monkeypatch.setattr(_metrics, "_instruments", instruments)
    return instruments


def app_with_a_path_parameter() -> FastAPI:
    app = FastAPI()
    install_conventions(app, readiness=Registry())

    @app.get("/things/{thing_id}")
    async def thing(thing_id: str) -> dict[str, str]:
        return {"id": thing_id}

    return app


async def request_to(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# --- cardinality ------------------------------------------------------------


async def test_the_route_label_is_the_pattern_not_the_path(recorders: dict[str, Recorder]) -> None:
    """THE cardinality test. Labelling with the URL gives one time series per
    id, which is millions for any real resource, and the backend falls over
    long before anyone notices the dashboard is wrong."""
    app = app_with_a_path_parameter()

    await request_to(app, "/things/abc")
    await request_to(app, "/things/def")

    routes = recorders[SERVER_REQUESTS].labels("route")

    assert routes == ["/things/{thing_id}", "/things/{thing_id}"]
    assert len(set(routes)) == 1, "two requests to one route produced two series"


async def test_an_unmatched_path_does_not_become_its_own_series(
    recorders: dict[str, Recorder],
) -> None:
    """A scanner probing random URLs would otherwise create a series per probe,
    which is a metrics outage anybody on the internet can cause."""
    app = app_with_a_path_parameter()

    await request_to(app, "/nope/one")
    await request_to(app, "/nope/two")

    assert set(recorders[SERVER_REQUESTS].labels("route")) == {"unmatched"}


async def test_no_label_carries_the_request_body_or_query(
    recorders: dict[str, Recorder],
) -> None:
    app = app_with_a_path_parameter()

    await request_to(app, "/things/abc?secret=hunter2")

    for recorder in recorders.values():
        for _value, attributes in recorder.points:
            assert "hunter2" not in str(attributes)


# --- what is recorded -------------------------------------------------------


async def test_a_served_request_records_a_count_and_a_duration(
    recorders: dict[str, Recorder],
) -> None:
    app = app_with_a_path_parameter()

    await request_to(app, "/things/abc")

    assert len(recorders[SERVER_REQUESTS].points) == 1
    assert len(recorders[SERVER_DURATION].points) == 1
    assert recorders[SERVER_REQUESTS].labels("status") == [200]


async def test_the_duration_matches_the_logged_one(recorders: dict[str, Recorder]) -> None:
    """Both come off one measurement. A dashboard disagreeing with the logs is
    half an hour of an incident spent working out which is lying."""
    app = app_with_a_path_parameter()

    await request_to(app, "/things/abc")

    duration, _ = recorders[SERVER_DURATION].points[0]
    assert duration >= 0


async def test_outbound_calls_are_labelled_by_upstream_not_url(
    recorders: dict[str, Recorder],
) -> None:
    from kit.clients import RetryPolicy, Upstream, service_client

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = service_client(
        Upstream(name="brain", base_url="http://brain", retry=RetryPolicy(attempts=1)),
        httpx.MockTransport(handle),
    )
    await client.get_json("/api/v1/policies/9f3a")
    await client.get_json("/api/v1/policies/2b71")
    await client.aclose()

    assert set(recorders[CLIENT_REQUESTS].labels("upstream")) == {"brain"}
    for _value, attributes in recorders[CLIENT_REQUESTS].points:
        assert "9f3a" not in str(attributes)


async def test_attempts_are_recorded_so_retry_amplification_is_visible(
    recorders: dict[str, Recorder],
) -> None:
    """The number worth alerting on. Three attempts per call against a
    struggling upstream is four times the traffic at the worst moment, and it
    is invisible in a plain request count."""
    from kit.clients import RetryPolicy, Upstream, service_client

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = service_client(
        Upstream(
            name="brain",
            base_url="http://brain",
            retry=RetryPolicy(attempts=3, backoff_seconds=0.001, backoff_cap_seconds=0.002),
        ),
        httpx.MockTransport(handle),
    )
    with pytest.raises(Exception, match="brain"):
        await client.get_json("/thing")
    await client.aclose()

    assert recorders[CLIENT_ATTEMPTS].points[0][0] == 3


# --- installation -----------------------------------------------------------


def test_recording_without_metrics_installed_is_silent() -> None:
    """The common case in tests and development. A metric call that raised
    when unconfigured would put telemetry into the request path's failure
    modes, which is exactly backwards."""
    shutdown_metrics()

    record(SERVER_REQUESTS, 1, route="/x", method="GET", status=200)


def test_no_endpoint_means_no_meter() -> None:
    """Unlike tracing, which installs a provider regardless. A tracer without
    an exporter still earns its keep by propagating context; a meter without
    one only accumulates numbers nobody will read."""
    assert start_metrics(service_name="s", version="v", environment="test") is False


def test_an_endpoint_installs_the_instruments() -> None:
    installed = start_metrics(
        service_name="s",
        version="v",
        environment="test",
        endpoint="http://127.0.0.1:4318/v1/metrics",
    )
    try:
        assert installed is True
        record(SERVER_REQUESTS, 1, route="/x", method="GET", status=200)
    finally:
        shutdown_metrics()


def test_a_histogram_and_a_counter_both_record() -> None:
    """They have different SDK methods, `record` and `add`, and dispatching on
    the wrong one raises inside the request path."""
    start_metrics(
        service_name="s",
        version="v",
        environment="test",
        endpoint="http://127.0.0.1:4318/v1/metrics",
    )
    try:
        record(SERVER_REQUESTS, 1, route="/x", method="GET", status=200)
        record(SERVER_DURATION, 12.5, route="/x", method="GET", status=200)
    finally:
        shutdown_metrics()


def test_metrics_stay_silent_without_the_otel_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service that never exports must not fail to start because a telemetry
    package is absent. It warns, because the operator asked for something they
    are not getting, and then serves."""
    import builtins

    real = builtins.__import__

    def missing(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("opentelemetry"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)

    assert (
        start_metrics(service_name="s", version="v", environment="test", endpoint="http://x:4318")
        is False
    )
