"""Two branches that only run when a caller does something slightly wrong."""

from __future__ import annotations

import io
import json
import logging

import httpx
from fastapi import FastAPI
from starlette.responses import StreamingResponse

from kit.health import Registry
from kit.httpapi import RateLimit, install_conventions
from kit.observability import JSONFormatter


def capture(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


def test_exc_info_outside_an_except_block_does_not_crash_the_formatter() -> None:
    """``exc_info=True`` with nothing raised gives ``(None, None, None)``.

    A formatter that assumed an exception was present would raise while trying
    to log, which turns a small mistake into a lost line at exactly the moment
    somebody was trying to record something.
    """
    logger, stream = capture("kit.test.noexc")

    logger.info("no exception is in flight", exc_info=True)

    line = json.loads(stream.getvalue().strip())
    assert line["msg"] == "no exception is in flight"
    assert "error_type" not in line


async def test_a_streamed_response_is_logged_with_the_bytes_it_sent() -> None:
    """A streaming body arrives as several messages, so the counter has to
    accumulate rather than record the last one."""
    logger, stream = capture("kit.test.stream")

    app = FastAPI()
    install_conventions(app, readiness=Registry(), rate_limit=RateLimit(), logger=logger)

    @app.get("/stream")
    async def streamed() -> StreamingResponse:
        async def body():  # type: ignore[no-untyped-def]
            yield b"one"
            yield b"two"

        return StreamingResponse(body())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/stream")

    assert response.text == "onetwo"
    lines = [json.loads(x) for x in stream.getvalue().splitlines() if x.strip()]
    request_lines = [x for x in lines if x.get("msg") == "HTTP request completed"]
    assert request_lines[-1]["bytes"] == 6
