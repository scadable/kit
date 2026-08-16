"""Boundary values and the branches that only run in unusual shapes."""

from __future__ import annotations

import io
import json
import logging

import httpx
import pytest
from fastapi import FastAPI

from kit.config import Service, load
from kit.health import Registry
from kit.httpapi import RateLimit, install_conventions
from kit.observability import JSONFormatter

SERVICE = Service(name="kit-service", env_prefix="KIT_")


def test_boundary_values_are_accepted() -> None:
    """One is a legal pool and a legal port; the check is `< 1`, not `<= 1`."""
    settings = load(SERVICE, {"KIT_DB_POOL_MAX": "1", "PORT": "1"})

    assert settings.database.pool_max == 1
    assert settings.process.port == 1
    assert settings.database.error is None


def test_a_fractional_timeout_is_accepted() -> None:
    """Sub-second timeouts are legitimate in a test harness."""
    settings = load(SERVICE, {"KIT_DB_HEALTH_TIMEOUT": "0.25"})

    assert settings.database.health_timeout_seconds == 0.25
    assert settings.database.error is None


def test_an_empty_declaration_name_is_ignored() -> None:
    """A blank name would create a dependency nothing can ever satisfy."""
    registry = Registry()
    registry.require("", "postgres")

    assert set(registry._required) == {"postgres"}


async def test_a_response_with_no_body_is_still_logged() -> None:
    """A 204 writes no body, and the line must still carry a byte count."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("kit.test.nobody")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)

    app = FastAPI()
    install_conventions(app, readiness=Registry(), rate_limit=RateLimit(), logger=logger)

    @app.delete("/thing", status_code=204)
    async def remove() -> None:
        return None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete("/thing")

    assert response.status_code == 204
    lines = [json.loads(x) for x in stream.getvalue().splitlines() if x.strip()]
    request_lines = [x for x in lines if x.get("msg") == "HTTP request completed"]
    assert request_lines[-1]["status"] == 204
    assert request_lines[-1]["bytes"] == 0


def test_a_record_with_no_exception_carries_no_error_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("kit.test.clean")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("nothing went wrong")

    line = json.loads(stream.getvalue().strip())
    assert "error" not in line
    assert "error_type" not in line


@pytest.mark.parametrize("value", ["0", "-3"])
def test_a_pool_size_below_one_is_refused(value: str) -> None:
    settings = load(SERVICE, {"KIT_DB_POOL_MAX": value})

    assert "at least 1" in str(settings.database.error)
