"""Logs are one JSON object per line, and carry what a search needs."""

from __future__ import annotations

import io
import json
import logging

import httpx
from fastapi import FastAPI

from kit.health import Registry
from kit.httpapi import RateLimit, install_conventions
from kit.observability import JSONFormatter, configure_logging


def capture() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("kit.test.capture")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, stream


def test_a_line_is_one_json_object() -> None:
    logger, stream = capture()

    logger.info("something happened", extra={"tenant": "acme"})

    line = json.loads(stream.getvalue().strip())
    assert line["msg"] == "something happened"
    assert line["level"] == "info"
    assert line["tenant"] == "acme"


def test_an_exception_is_summarised_not_dumped() -> None:
    """A traceback in a JSON field is unreadable, and the same exception reaches
    the error tracker with its frames intact."""
    logger, stream = capture()

    try:
        raise ValueError("the cause")
    except ValueError:
        logger.exception("it failed")

    line = json.loads(stream.getvalue().strip())
    assert line["error_type"] == "ValueError"
    assert line["error"] == "the cause"
    assert "Traceback" not in stream.getvalue()


async def test_the_request_line_carries_the_route_pattern_not_the_url() -> None:
    """A path carries tokens and ids, and a log line is the wrong place to
    accumulate them."""
    stream = io.StringIO()
    configure_logging("info", stream)

    app = FastAPI()
    install_conventions(app, readiness=Registry(), rate_limit=RateLimit())

    @app.get("/policies/{policy_id}")
    async def one(policy_id: str) -> dict[str, str]:
        return {"id": policy_id}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/policies/secret-token-value")

    lines = [json.loads(x) for x in stream.getvalue().splitlines() if x.strip()]
    request_lines = [x for x in lines if x.get("msg") == "HTTP request completed"]

    assert request_lines, "no request line was logged"
    line = request_lines[-1]
    assert line["route"] == "/policies/{policy_id}"
    assert "secret-token-value" not in json.dumps(line)
    assert line["status"] == 200
    assert line["method"] == "GET"
    assert "duration_ms" in line
    assert line["request_id"]


async def test_an_unmatched_path_is_logged_as_unmatched() -> None:
    """Worth seeing: a scan that costs less than a real request is a scan
    somebody runs."""
    stream = io.StringIO()
    configure_logging("info", stream)

    app = FastAPI()
    install_conventions(app, readiness=Registry(), rate_limit=RateLimit())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/nope")

    lines = [json.loads(x) for x in stream.getvalue().splitlines() if x.strip()]
    request_lines = [x for x in lines if x.get("msg") == "HTTP request completed"]

    assert request_lines[-1]["route"] == "unmatched"
