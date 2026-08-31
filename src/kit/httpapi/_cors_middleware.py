"""CORS, except on the paths that are not browser surfaces."""

from __future__ import annotations

from typing import Any

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from kit.httpapi._prefixes import under


class ScopedCORSMiddleware:
    """``CORSMiddleware``, skipped entirely under a trusted prefix.

    WHY A SERVICE WOULD WANT THIS. An operator or fleet surface reached only by
    another server holding a bearer is not a browser surface. Answering it with
    ``access-control-allow-origin`` names an origin whose browser may read the
    response, and for those paths there is no such origin and never will be. The
    header is a statement about who may call, and on those paths the statement is
    false.

    BE PRECISE ABOUT WHAT THIS DOES NOT DO, because the tempting claim is wrong
    and would be believed. It does NOT hide the paths. ``CORSMiddleware`` answers
    a preflight without consulting the router, so ``OPTIONS`` on a path that
    matches no route already returns 200 with the allow-list; exempting a prefix
    changes nothing a prober can see, because an exempt path and a nonexistent
    one look alike either way. What it removes is the header on a REAL response,
    which is the false statement, not a disclosure.

    Nor is it authorization. A path under a trusted prefix is exactly as reachable
    as it was; CORS never restricted a server-side caller, only a browser. This
    narrows what the service SAYS, and the credential on the route is still the
    entire boundary.

    The inner app is wrapped twice on purpose: ``self.app`` is the chain without
    CORS and ``self.cors`` is the same chain with it, so dispatch is a prefix test
    and neither path pays for the other.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        options: dict[str, Any],
        prefixes: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.cors = CORSMiddleware(app, **options)
        self.prefixes = prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and under(scope, self.prefixes):
            await self.app(scope, receive, send)
            return
        await self.cors(scope, receive, send)
