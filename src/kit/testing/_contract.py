"""The shared assertions, callable from any service's test suite."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import httpx
from fastapi import FastAPI

from kit.httpapi import REQUEST_ID_HEADER


async def assert_contract(app: FastAPI) -> None:
    """Assert this app answers the way every service must.

    Call it from a test in your own suite::

        async def test_it_meets_the_contract() -> None:
            await assert_contract(create_app())

    Everything checked here is fleet-wide behaviour. If one of these fails, the
    service has not customised itself, it has desynchronised from every dashboard
    and client that reads it.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _assert_liveness(client)
        await _assert_readiness(client)
        await _assert_envelope(client)
        await _assert_request_id(client)


async def _assert_liveness(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200, "/healthz must answer 200 while the process lives"
    assert response.json() == {"status": "ok"}


async def _assert_readiness(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code in (200, 503), (
        "/readyz must answer 200 when ready and 503 when not, because a readiness "
        "probe reads the status code and nothing else"
    )

    body = response.json()
    assert set(body) == {"status", "checks"}, "/readyz must answer {status, checks}"
    assert body["status"] in ("ready", "not_ready")
    assert isinstance(body["checks"], dict), (
        "checks must be a flat map of name to word; nesting an object here breaks "
        "every consumer comparing checks[name] == 'error' at once"
    )
    for name, state in body["checks"].items():
        assert state in ("ready", "error", "unconfigured"), (
            f"{name} reports {state!r}, which is not one of the three states"
        )

    expected = 200 if body["status"] == "ready" else 503
    assert response.status_code == expected, (
        f"status {body['status']!r} was sent with HTTP {response.status_code}"
    )


async def _assert_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/this-route-does-not-exist")
    assert response.status_code == 404

    body = response.json()
    assert set(body) == {"error", "request_id"}, (
        "an unrouted path must answer the house envelope. Registering FastAPI's "
        "HTTPException subclass instead of Starlette's base leaves this answering "
        "{'detail': ...}, because the router raises the base class first"
    )
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"], "the error code is what clients branch on"


async def _assert_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/this-route-does-not-exist")
    header = response.headers.get(REQUEST_ID_HEADER, "")
    assert header, f"{REQUEST_ID_HEADER} must be on every response"
    assert response.json()["request_id"] == header, (
        "the envelope's request_id must match the header, or a customer quoting "
        "one cannot be matched to the log line carrying the other"
    )

    given = "an-upstream-request-id"
    passed = await client.get("/this-route-does-not-exist", headers={REQUEST_ID_HEADER: given})
    assert passed.headers.get(REQUEST_ID_HEADER) == given, (
        "an inbound request id must be honoured so a trace spans services"
    )


def contract_tests(
    app_factory: Callable[[], FastAPI],
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Build a test function a service can drop into its suite.

    test_contract = contract_tests(create_app)
    """

    async def test_contract() -> None:
        await assert_contract(app_factory())

    return test_contract
