"""The middleware chain. Import from ``kit.httpapi``.

The ORDER is the contract, not the individual pieces. It is assembled by one
function rather than exported as six, because a service assembling these by hand
would eventually get the order subtly wrong and nothing would fail loudly when it
did.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kit.httpapi._envelope import (
    INTERNAL_ERROR,
    error_response,
    request_id_var,
)

Dispatch = Callable[[Request], Awaitable[Response]]

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware:
    """Assigns the request id and echoes it on the response.

    First in the chain, because everything downstream logs it or answers with
    it. An inbound id is honoured so a trace spans services; otherwise one is
    generated.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        assigned = incoming or uuid.uuid4().hex
        token = request_id_var.set(assigned)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((REQUEST_ID_HEADER.lower().encode(), assigned.encode()))
                # Set before any handler can write a body.
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"referrer-policy", b"no-referrer"))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


class RequestLogMiddleware:
    """One structured line per request, after the handler returns.

    Inside the request id so the line carries it; outside the recoverer so a
    panicking request is still logged as a completed 500 rather than vanishing.
    """

    def __init__(self, app: ASGIApp, logger: logging.Logger | None = None) -> None:
        self.app = app
        self.logger = logger or logging.getLogger("kit.httpapi")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 200
        written = 0

        async def send_observed(message: Message) -> None:
            nonlocal status, written
            if message["type"] == "http.response.start":
                status = message["status"]
            # No branch on the message type: only body messages carry a body, so
            # the default covers every other kind, including the trailers a
            # future protocol might add.
            written += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_observed)
        finally:
            self.logger.info(
                "HTTP request completed",
                extra={
                    "request_id": request_id_var.get(),
                    "method": scope.get("method", ""),
                    "route": _route_pattern(scope),
                    "status": status,
                    "bytes": written,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )


def _route_pattern(scope: Scope) -> str:
    """The route PATTERN, never the URL.

    A path carries tokens and ids, and a log line is the wrong place to
    accumulate them. Requests that matched nothing log ``unmatched``, which is
    itself worth seeing: a scan that costs less than a real request is a scan
    somebody runs.
    """
    route = scope.get("route")
    pattern = getattr(route, "path", "")
    return pattern or "unmatched"


class RecoveryMiddleware:
    """Turns an unhandled exception into the house envelope.

    Inside CORS, so the 500 still carries the CORS headers: without them a
    browser refuses to expose the response and a server error reads to the
    frontend as a network failure instead. Outside the rate limiter, so a
    limiter that raises becomes a 500 rather than a dropped connection, which is
    the direction this must fail in.
    """

    def __init__(self, app: ASGIApp, logger: logging.Logger | None = None) -> None:
        self.app = app
        self.logger = logger or logging.getLogger("kit.httpapi")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_tracking(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking)
        except Exception:
            self.logger.exception(
                "HTTP handler raised",
                extra={"request_id": request_id_var.get()},
            )
            if started:
                # The response is already on the wire. Leave the partial answer
                # alone rather than corrupting it with a second status line.
                raise
            response = error_response(500, INTERNAL_ERROR, "An internal error occurred.")
            await response(scope, receive, send)
