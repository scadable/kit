"""The last behaviours: credential identity, trace correlation, sweeping."""

from __future__ import annotations

import io
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from kit.config import Cache, Service, load
from kit.health import Registry
from kit.httpapi import install_conventions, json_error
from kit.httpapi._ratelimit import SESSION_COOKIE, Limiter
from kit.httpapi._ratelimit import RateLimit as RL
from kit.httpapi._ratelimit_middleware import identify
from kit.observability import JSONFormatter

SERVICE = Service(name="kit-service", env_prefix="KIT_")


def request_with(headers: dict[str, str], cookies: str = "") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    if cookies:
        raw.append((b"cookie", cookies.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": raw,
            "client": ("192.0.2.10", 1234),
            "query_string": b"",
        }
    )


def test_an_authorization_header_identifies_a_caller() -> None:
    credential, address = identify(request_with({"authorization": "Bearer token"}))

    assert credential.startswith("c:")
    assert "token" not in credential, "a live credential must never be a map key"
    assert address == "192.0.2.10"


def test_a_session_cookie_identifies_a_caller_when_there_is_no_header() -> None:
    credential, _ = identify(request_with({}, cookies=f"{SESSION_COOKIE}=abc123"))

    assert credential.startswith("s:")
    assert "abc123" not in credential


def test_an_anonymous_request_has_no_credential() -> None:
    credential, address = identify(request_with({}))

    assert credential == ""
    assert address == "192.0.2.10"


def test_the_header_wins_over_the_cookie() -> None:
    """One identity per request, chosen deterministically."""
    credential, _ = identify(
        request_with({"authorization": "Bearer t"}, cookies=f"{SESSION_COOKIE}=c")
    )

    assert credential.startswith("c:")


def test_idle_buckets_are_swept_so_the_table_does_not_grow_forever() -> None:
    """After one window an idle bucket has refilled completely, so it holds what
    a new one would and dropping it changes no decision."""
    clock_value = [1000.0]
    limiter = Limiter(
        limit=RL(requests=60, window_seconds=60, burst=5),
        now=lambda: clock_value[0],
    )
    limiter.allow(limiter.charges("", "1.2.3.4"))
    assert len(limiter._buckets) == 1

    # Past twice the window, then a write to trigger the inline sweep.
    clock_value[0] += 200
    limiter.allow(limiter.charges("", "5.6.7.8"))

    assert "a:1.2.3.4" not in limiter._buckets


def test_a_disabled_limiter_short_circuits() -> None:
    from kit.httpapi import unlimited

    limiter = Limiter(limit=unlimited().resolved())

    # resolve() turns an off policy into the default, so the middleware, not the
    # limiter, is what honours "off". Proven by the middleware test; here we only
    # assert the table stays usable.
    assert limiter.allow(limiter.charges("", "1.2.3.4"))[0] is True


def test_the_trace_fields_are_absent_without_an_active_span() -> None:
    """A service that exports nothing still logs; it simply logs without
    correlation."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("kit.test.notrace")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("no span here")

    line = json.loads(stream.getvalue().strip())
    assert "trace_id" not in line


def test_the_trace_fields_appear_inside_a_span() -> None:
    """Injected on every line rather than by the caller, because a correlation
    that depends on remembering is missing from the line you need."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    trace.set_tracer_provider(TracerProvider())

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("kit.test.trace")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)

    with trace.get_tracer("test").start_as_current_span("work"):
        logger.info("inside a span")

    line = json.loads(stream.getvalue().strip())
    assert len(line["trace_id"]) == 32
    assert len(line["span_id"]) == 16


def test_json_error_builds_one_envelope_directly() -> None:
    """For a handler that would rather return than raise."""
    response = json_error(409, "conflict", "That already exists.")

    assert response.status_code == 409
    assert json.loads(bytes(response.body))["error"]["code"] == "conflict"


def test_an_informational_dependency_can_be_recorded_as_unconfigured() -> None:
    registry = Registry()
    registry.add_informational_unconfigured("harness", "no endpoint for that tenant")

    assert len(registry._entries) == 1


def test_a_cache_block_is_enabled_once_configured() -> None:
    assert Cache().enabled is False
    assert Cache(url="rediss://cache:6379").enabled is True


def test_a_pool_size_below_one_is_refused() -> None:
    from kit.config import ConfigError

    settings = load(SERVICE, {"KIT_DB_POOL_MAX": "0"})

    assert isinstance(settings.database.error, ConfigError)


async def test_the_limiter_is_skipped_entirely_when_unlimited() -> None:
    from kit.httpapi import unlimited

    app = FastAPI()
    install_conventions(app, readiness=Registry(), rate_limit=unlimited())

    @app.get("/thing")
    async def thing() -> dict[str, bool]:
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        statuses = [(await client.get("/thing")).status_code for _ in range(30)]

    assert set(statuses) == {200}


async def test_no_rate_limit_argument_means_no_limiter_installed() -> None:
    app = FastAPI()
    install_conventions(app, readiness=Registry())

    @app.get("/thing")
    async def thing() -> dict[str, bool]:
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/thing")).status_code == 200


def test_a_default_log_level_is_used_for_an_unknown_name() -> None:
    from kit.observability import configure_logging

    stream = io.StringIO()
    configure_logging("shouty", stream)

    assert logging.getLogger().level == logging.INFO
