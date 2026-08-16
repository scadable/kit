"""The inheritable contract test, and the two cross-origin modes."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from kit.health import Registry, failing_check
from kit.httpapi import CORS, RateLimit, install_conventions, normalize_origins
from kit.testing import assert_contract, contract_tests


async def ok() -> None:
    return None


def app_with(registry: Registry, cors: CORS | None = None) -> FastAPI:
    app = FastAPI()
    install_conventions(app, readiness=registry, cors=cors, rate_limit=RateLimit())
    return app


async def test_a_correctly_wired_app_meets_the_contract() -> None:
    registry = Registry()
    registry.add("postgres", ok)

    await assert_contract(app_with(registry))


async def test_the_contract_holds_when_a_dependency_is_down() -> None:
    """503 and not_ready must agree, in both directions."""
    registry = Registry()
    registry.add("postgres", failing_check(RuntimeError("down")))

    await assert_contract(app_with(registry))


async def test_contract_tests_builds_a_runnable_test() -> None:
    test = contract_tests(lambda: app_with(Registry()))

    await test()


async def test_the_contract_catches_an_app_that_did_not_install_the_kit() -> None:
    """The helper has to actually fail, or a service could import it, pass, and
    have inherited nothing."""
    bare = FastAPI()

    with pytest.raises(AssertionError):
        await assert_contract(bare)


def test_origins_are_normalised() -> None:
    """A service reading a comma-separated variable that happens to be empty
    hands over a one-element list holding an empty string."""
    assert normalize_origins(["  https://app.example.com/  "]) == {"https://app.example.com"}
    assert normalize_origins([""]) == set()
    assert normalize_origins(["  "]) == set()
    assert normalize_origins([]) == set()


def test_the_two_cors_modes_cannot_both_be_set() -> None:
    """Raising at construction means a service configured into a contradiction
    fails to start rather than serving whichever mode happened to win."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        CORS(allowed_origins=["https://app.example.com"], public_read=True)


def test_an_empty_origin_list_is_not_a_contradiction() -> None:
    """Otherwise an unset environment variable crashes a correctly configured
    service."""
    assert CORS(allowed_origins=[""], public_read=True).public_read is True
    assert CORS(allowed_origins=["  "], public_read=True).enabled is True


def test_cors_is_off_until_it_is_configured() -> None:
    assert CORS().enabled is False
    assert CORS(allowed_origins=["https://app.example.com"]).enabled is True


async def test_an_allowed_origin_gets_credentials() -> None:
    app = app_with(Registry(), CORS(allowed_origins=["https://app.example.com"]))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz", headers={"Origin": "https://app.example.com"})

    assert response.headers["access-control-allow-origin"] == "https://app.example.com"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "X-Request-ID" in response.headers["access-control-expose-headers"]


async def test_an_unknown_origin_gets_no_cors_headers() -> None:
    """The browser enforces the refusal, and answering with an explicit rejection
    would tell a prober that the endpoint exists and is origin-checked."""
    app = app_with(Registry(), CORS(allowed_origins=["https://app.example.com"]))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz", headers={"Origin": "https://evil.net"})

    assert "access-control-allow-origin" not in response.headers


async def test_public_read_is_a_wildcard_and_never_credentials() -> None:
    """Take the credential away and the argument against a wildcard evaporates:
    there is nothing to attach, so nothing an attacker's page learns by asking
    that it could not learn from its own server."""
    app = app_with(Registry(), CORS(public_read=True))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz", headers={"Origin": "https://anyone.net"})

    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
