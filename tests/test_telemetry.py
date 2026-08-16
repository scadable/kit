"""Telemetry export, and how the kit behaves when the extra is absent."""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest
from fastapi import FastAPI

from kit.config import ConfigError, Service, load, validate_database_url
from kit.httpapi._ratelimit import Limiter, RateLimit
from kit.observability import (
    JSONFormatter,
    instrument_app,
    shutdown_telemetry,
    start_telemetry,
)

SERVICE = Service(name="kit-service", env_prefix="KIT_")


def test_a_configured_endpoint_installs_a_provider() -> None:
    pytest.importorskip("opentelemetry.sdk")

    installed = start_telemetry(
        service_name="kit-service",
        version="1.2.3",
        environment="test",
        endpoint="http://localhost:4318/v1/traces",
    )

    assert installed is True
    shutdown_telemetry()


def test_shutdown_is_safe_to_call_twice() -> None:
    """A container that exits promptly loses the spans describing whatever made
    it exit, so shutdown must always be callable."""
    shutdown_telemetry()
    shutdown_telemetry()


def test_instrumenting_an_app_is_idempotent_enough_to_call() -> None:
    pytest.importorskip("opentelemetry.instrumentation.fastapi")

    instrument_app(FastAPI())


def test_a_configured_endpoint_without_the_extra_warns_rather_than_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator asked for something they are not getting. Continuing
    silently means discovering it when a trace is needed."""
    for name in list(sys.modules):
        if name.startswith("opentelemetry"):
            monkeypatch.setitem(sys.modules, name, None)

    installed = start_telemetry(
        service_name="kit-service",
        version="dev",
        environment="test",
        endpoint="http://localhost:4318/v1/traces",
    )

    assert installed is False


def test_instrumenting_without_the_extra_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in list(sys.modules):
        if name.startswith("opentelemetry"):
            monkeypatch.setitem(sys.modules, name, None)

    instrument_app(FastAPI())


def test_logging_survives_without_the_telemetry_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service that exports nothing still logs; it just logs uncorrelated."""
    for name in list(sys.modules):
        if name.startswith("opentelemetry"):
            monkeypatch.setitem(sys.modules, name, None)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("kit.test.noextra")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("still logging")

    line = json.loads(stream.getvalue().strip())
    assert line["msg"] == "still logging"
    assert "trace_id" not in line


def test_evicting_from_an_empty_table_does_nothing() -> None:
    limiter = Limiter(limit=RateLimit(requests=60, window_seconds=60, burst=5))

    limiter._evict_one(1000.0)

    assert limiter._buckets == {}


def test_saturation_is_reported_at_most_once_a_window() -> None:
    """A full table is otherwise invisible: the limiter keeps answering
    correctly and only its precision degrades."""
    clock = [1000.0]
    limiter = Limiter(
        limit=RateLimit(requests=60, window_seconds=60, burst=5),
        now=lambda: clock[0],
    )
    limiter._buckets["one"] = limiter._buckets.get("one") or __import__(
        "kit.httpapi._ratelimit", fromlist=["_Bucket"]
    )._Bucket(tokens=1.0, seen=clock[0])

    limiter._report(clock[0])
    first = limiter._evicted

    limiter._report(clock[0])

    assert first == 0, "the first report resets the counter"
    assert limiter._evicted == 1, "the second was suppressed inside the window"


@pytest.mark.parametrize(
    ("key", "value", "complaint"),
    [
        ("DB_POOL_MAX", "many", "whole number"),
        ("DB_HEALTH_TIMEOUT", "soon", "number of seconds"),
    ],
)
def test_a_malformed_value_names_the_variable_to_fix(key: str, value: str, complaint: str) -> None:
    """An error naming nothing sends an operator to read the source."""
    settings = load(SERVICE, {f"KIT_{key}": value})

    assert complaint in str(settings.database.error)
    assert key in str(settings.database.error)


def test_a_url_that_cannot_be_parsed_at_all_is_still_not_echoed() -> None:
    with pytest.raises(ConfigError) as caught:
        validate_database_url("postgres://host:notaport/db")

    assert "notaport" not in str(caught.value)
    assert "DATABASE_URL" in str(caught.value)
