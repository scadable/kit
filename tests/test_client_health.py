"""Upstream state in /readyz, without it deciding readiness.

The intuitive choice is the wrong one and it is worth a test saying so: if an
upstream is down, marking this service not-ready removes every one of its
replicas from the load balancer. That does not fix the upstream. It takes a
service that could still serve most of its routes and removes it too, so one
dependency's outage becomes two services' outage and the recovery needs both.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from kit.clients import BreakerPolicy, RetryPolicy, Upstream, register_upstreams, service_client
from kit.health import Registry
from kit.httpapi import install_conventions

FAST = RetryPolicy(attempts=1, backoff_seconds=0.001, backoff_cap_seconds=0.002)


def failing_client(name: str = "brain"):
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = service_client(
        Upstream(
            name=name,
            base_url=f"http://{name}",
            retry=FAST,
            breaker=BreakerPolicy(failure_threshold=1, recovery_seconds=60),
        ),
        httpx.MockTransport(handle),
    )
    return client


async def readiness_of(registry: Registry) -> httpx.Response:
    app = FastAPI()
    install_conventions(app, readiness=registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/readyz")


async def test_a_shed_upstream_is_visible_but_does_not_block() -> None:
    """THE decision, asserted. 200 with the upstream shown as failing: an
    incident responder sees which dependency is being shed, and Kubernetes
    keeps sending this service traffic it can still serve."""
    registry = Registry()
    client = failing_client()
    register_upstreams(registry, [client])

    with pytest.raises(Exception, match="brain"):
        await client.get_json("/thing")
    assert client.breaker.is_open, "the breaker did not open, so this proves nothing"

    response = await readiness_of(registry)
    await client.aclose()

    assert response.status_code == 200, "a shed upstream took the service out of rotation"
    assert response.json()["status"] == "ready"
    assert response.json()["checks"]["brain"] == "error"


async def test_a_healthy_upstream_reports_ready() -> None:
    registry = Registry()

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = service_client(
        Upstream(name="brain", base_url="http://brain", retry=FAST), httpx.MockTransport(handle)
    )
    register_upstreams(registry, [client])

    response = await readiness_of(registry)
    await client.aclose()

    assert response.json()["checks"]["brain"] == "ready"


async def test_the_check_does_not_call_the_upstream() -> None:
    """A readiness probe that calls every dependency turns one kubelet's
    polling interval into load on five other services, from every replica,
    forever. It reports the breaker's opinion instead."""
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    registry = Registry()
    client = service_client(
        Upstream(name="brain", base_url="http://brain", retry=FAST), httpx.MockTransport(handle)
    )
    register_upstreams(registry, [client])

    await readiness_of(registry)
    await client.aclose()

    assert calls == [], "the readiness check made a network request"


async def test_several_upstreams_each_get_their_own_line() -> None:
    """One line per dependency, so the report answers WHICH one rather than
    that something is wrong."""
    registry = Registry()
    clients = [failing_client("brain"), failing_client("policy")]
    register_upstreams(registry, clients)

    response = await readiness_of(registry)
    for client in clients:
        await client.aclose()

    assert {"brain", "policy"} <= set(response.json()["checks"])
