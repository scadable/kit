"""The ASGI half of the rate limiter."""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from kit.httpapi._envelope import RATE_LIMITED, error_response
from kit.httpapi._ratelimit import (
    SESSION_COOKIE,
    Limiter,
    RateLimit,
    client_address,
    fingerprint,
)


def identify(request: Request, trusted_proxies: int = 0) -> tuple[str, str]:
    """The caller's bucket key and address.

    Prefixes keep kinds from colliding: without them a session cookie whose hash
    happened to equal an address string would share a bucket, and the address's
    two roles would become one bucket charged at two rates.
    """
    headers = {key.lower(): value for key, value in request.headers.items()}
    peer = request.client.host if request.client else ""
    address = client_address(headers, peer, trusted_proxies)

    authorization = headers.get("authorization", "")
    if authorization:
        return f"c:{fingerprint(authorization)}", address

    session = request.cookies.get(SESSION_COOKIE, "")
    if session:
        return f"s:{fingerprint(session)}", address

    return "", address


EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})
"""Paths the limiter must never refuse.

These are the orchestrator's questions, not a caller's. A 429 on /healthz
restarts a container that was working, and a 429 on /readyz takes a healthy pod
out of the Service endpoints. Under exactly the traffic spike a rate limit exists
to survive, limiting the probes turns a busy service into a shrinking one, which
is the opposite of what was wanted.

They are also cheap and unauthenticated by design, so there is nothing here worth
protecting with a bucket.
"""


class RateLimitMiddleware:
    """Refuse with a fully formed answer, or pass through."""

    def __init__(
        self,
        app: ASGIApp,
        limit: RateLimit,
        logger: logging.Logger | None = None,
        exempt_paths: frozenset[str] = EXEMPT_PATHS,
    ) -> None:
        self.app = app
        self.log = logger or logging.getLogger("kit.httpapi")
        self.limiter = Limiter(limit=limit, logger=self.log)
        self.enabled = not limit.off
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not self.enabled
            or scope.get("path", "") in self.exempt_paths
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        credential, address = identify(request, self.limiter.limit.trusted_proxies)
        allowed, retry_after, by_address = self.limiter.allow(
            self.limiter.charges(credential, address)
        )

        if allowed:
            await self.app(scope, receive, send)
            return

        # The two refusals need different words. A caller refused by its own
        # bucket is being told to slow down, which it can act on. A caller
        # refused by the address ceiling may be behaving perfectly and sharing an
        # address with something that is not, and telling that person to slow
        # down is a lie.
        message = (
            "Too many requests from this network. Please try again shortly."
            if by_address
            else "Too many requests. Please slow down and try again shortly."
        )
        response = error_response(429, RATE_LIMITED, message)
        response.headers["Retry-After"] = str(retry_after)
        await response(scope, receive, send)
