"""The readiness contract, which is the reason this package exists."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from kit.health import Registry, failing_check
from kit.httpapi import CORS, RateLimit, install_conventions


async def ok() -> None:
    return None


def build(registry: Registry) -> FastAPI:
    app = FastAPI()
    install_conventions(app, readiness=registry, cors=CORS(), rate_limit=RateLimit())
    return app


async def call(app: FastAPI, path: str, **kwargs: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, **kwargs)  # type: ignore[arg-type]


async def test_healthz_never_touches_a_dependency() -> None:
    """A failing liveness probe restarts the container, so a database blip must
    not be able to reach it."""
    registry = Registry()
    registry.add("postgres", failing_check(RuntimeError("down")))

    response = await call(build(registry), "/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_is_200_when_everything_answers() -> None:
    registry = Registry()
    registry.add("postgres", ok)
    registry.add_optional_unconfigured("valkey", "no cache configured")

    response = await call(build(registry), "/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": "ready", "valkey": "unconfigured"},
    }


async def test_readyz_is_503_when_a_dependency_is_down() -> None:
    """The status code carries the verdict because a kubelet reads nothing else.

    A handler that always answered 200 would never be taken out of the Service
    endpoints, which is the entire purpose of the endpoint.
    """
    registry = Registry()
    registry.add("postgres", failing_check(RuntimeError("connection refused")))

    response = await call(build(registry), "/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["postgres"] == "error"


async def test_readyz_never_serializes_the_cause() -> None:
    """Readiness is unauthenticated at the ingress and driver errors embed
    internal hostnames, addresses and ports."""
    registry = Registry()
    registry.add("postgres", failing_check(RuntimeError("host=10.0.0.4 password=hunter2")))

    response = await call(build(registry), "/readyz")

    assert "10.0.0.4" not in response.text
    assert "hunter2" not in response.text


async def test_checks_is_present_even_when_empty() -> None:
    """An empty object says "asked, nothing is registered", which is a different
    and useful fact from a missing key. A missing key is indistinguishable from
    an old build still being deployed."""
    response = await call(build(Registry()), "/readyz")

    assert response.json() == {"status": "ready", "checks": {}}


async def test_a_required_dependency_that_was_never_wired_blocks() -> None:
    """The root-cause guard. Registration inside a success branch means the
    failure path returns early and the dependency disappears from the report."""
    registry = Registry()
    registry.require("postgres", "auth")
    registry.add("postgres", ok)

    response = await call(build(registry), "/readyz")

    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "ready", "auth": "unconfigured"}


@pytest.mark.parametrize(
    ("register", "expected_status"),
    [
        ("add_unconfigured", 503),
        ("add_optional_unconfigured", 200),
    ],
)
async def test_required_unconfigured_blocks_and_optional_does_not(
    register: str, expected_status: int
) -> None:
    """Visible either way. That is the point: it must appear in the report
    whether or not it is fatal."""
    registry = Registry()
    getattr(registry, register)("auth", "no domain configured")

    response = await call(build(registry), "/readyz")

    assert response.status_code == expected_status
    assert response.json()["checks"] == {"auth": "unconfigured"}
