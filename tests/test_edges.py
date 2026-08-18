"""The paths that only run when something is wrong or unusual."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from starlette.websockets import WebSocket

from kit.config import ConfigError, Service, load, validate_database_url
from kit.health import Registry
from kit.httpapi import RateLimit, install_conventions
from kit.httpapi._handlers import default_code_for
from kit.observability import instrument_app, shutdown_telemetry, start_telemetry

SERVICE = Service(name="kit-service", env_prefix="KIT_")


@pytest.mark.parametrize(
    ("url", "complaint"),
    [
        ("mysql://host/db", "scheme"),
        ("postgres:///db", "no host"),
        ("postgres://host/", "no database"),
    ],
)
def test_a_database_url_is_checked_before_use(url: str, complaint: str) -> None:
    with pytest.raises(ConfigError, match=complaint):
        validate_database_url(url)


def test_a_valid_database_url_passes_through_unchanged() -> None:
    url = "postgresql://user:pw@db.example.com:25060/app?sslmode=require"

    assert validate_database_url(url) == url


@pytest.mark.parametrize("key", ["DB_POOL_MAX", "DB_HEALTH_TIMEOUT"])
def test_a_malformed_store_setting_degrades_rather_than_crashes(key: str) -> None:
    settings = load(SERVICE, {f"KIT_{key}": "soon"})

    assert settings.database.error is not None
    assert settings.database.enabled is True


def test_a_malformed_cache_setting_degrades_and_keeps_its_prefix() -> None:
    settings = load(SERVICE, {"KIT_CACHE_HEALTH_TIMEOUT": "-1"})

    assert settings.cache.error is not None
    assert settings.cache.key_prefix == "kit-service"


def test_a_malformed_process_timeout_stops_startup() -> None:
    with pytest.raises(ConfigError, match="SHUTDOWN_TIMEOUT"):
        load(SERVICE, {"KIT_SHUTDOWN_TIMEOUT": "later"})


def test_a_negative_process_timeout_stops_startup() -> None:
    with pytest.raises(ConfigError, match="greater than zero"):
        load(SERVICE, {"KIT_MIGRATION_TIMEOUT": "-5"})


def test_a_service_without_a_name_is_loudly_unnamed() -> None:
    """Telemetry from several services lands in one backend, and this on a
    dashboard is a bug report rather than a default worth living with."""
    settings = load(Service(name="", env_prefix="KIT_"), {})

    assert settings.process.service_name == "unknown-service"


def test_settings_built_by_hand_do_not_explode_on_value() -> None:
    from kit.config import Settings

    assert Settings(service=SERVICE).value("ANYTHING", "fallback") == "fallback"


def test_an_unmapped_status_becomes_internal_error() -> None:
    """Fail closed: a status nobody mapped is not silently reported as a client
    error."""
    assert default_code_for(404) == "not_found"
    assert default_code_for(418) == "internal_error"


async def test_a_websocket_passes_through_the_chain_untouched() -> None:
    """The middleware is HTTP-shaped; a non-HTTP scope must not be rewritten."""
    app = FastAPI()
    install_conventions(app, readiness=Registry(), rate_limit=RateLimit())

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_text("hello")
        await socket.close()

    from fastapi.testclient import TestClient

    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        assert socket.receive_text() == "hello"


async def test_a_handler_that_already_answered_is_not_overwritten() -> None:
    """A partial response on the wire must not be corrupted with a second status
    line."""
    app = FastAPI()
    install_conventions(app, readiness=Registry(), rate_limit=RateLimit())

    @app.get("/partial")
    async def partial() -> object:
        from starlette.responses import StreamingResponse

        async def body():  # type: ignore[no-untyped-def]
            yield b"partial"
            raise RuntimeError("failed after the body started")

        return StreamingResponse(body(), status_code=202)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/partial")

    assert response.status_code == 202


def test_telemetry_is_installed_without_an_endpoint() -> None:
    """No endpoint means no EXPORTER. It does not mean no provider.

    This test used to assert the opposite and its docstring claimed trace
    context still propagated, which is the combination that hid the bug: the
    provider was never installed, so nothing propagated and no log line carried
    a trace id, in exactly the environments where somebody reads logs by hand.

    tests/test_propagation.py asserts the behaviour this enables; what is
    checked here is that the switch is on.
    """
    installed = start_telemetry(
        service_name="kit-service", version="dev", environment="test", endpoint=""
    )
    shutdown_telemetry()

    assert installed is True


def test_instrumenting_without_the_extra_is_a_no_op() -> None:
    instrument_app(FastAPI())
    shutdown_telemetry()
