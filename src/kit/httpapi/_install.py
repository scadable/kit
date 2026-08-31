"""Assemble the chain. This is where the order lives."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from kit.health import Registry
from kit.httpapi._cors import (
    CORS,
    CREDENTIALED_HEADERS,
    CREDENTIALED_METHODS,
    EXPOSED_HEADERS,
    MAX_AGE_SECONDS,
    PUBLIC_READ_HEADERS,
    PUBLIC_READ_METHODS,
    normalize_origins,
)
from kit.httpapi._cors_middleware import ScopedCORSMiddleware
from kit.httpapi._handlers import CodeMapper, default_code_for, install_error_handlers
from kit.httpapi._middleware import (
    RecoveryMiddleware,
    RequestIDMiddleware,
    RequestLogMiddleware,
)
from kit.httpapi._prefixes import normalize_trusted_prefixes
from kit.httpapi._probes import probe_router
from kit.httpapi._ratelimit import RateLimit
from kit.httpapi._ratelimit_middleware import RateLimitMiddleware


def _options(**kwargs: object) -> dict[str, object]:
    """The CORSMiddleware keyword arguments, as a dict it can be splatted from.

    A named helper rather than a literal so the two branches below still read as
    argument lists rather than as dictionaries, which is what makes the
    difference between them, credentials against wildcard, legible at a glance.
    """
    return kwargs


def install_conventions(
    app: FastAPI,
    *,
    readiness: Registry,
    cors: CORS | None = None,
    rate_limit: RateLimit | None = None,
    trusted_prefixes: tuple[str, ...] = (),
    logger: logging.Logger | None = None,
    code_for: CodeMapper = default_code_for,
) -> None:
    """Install the whole chain, the probes and the error handlers.

    Take this and you get the conventions. Skip it and assemble the pieces
    yourself in your own order, minus whatever you do not want: kit never owns
    your application, so opting out of a piece is not calling it.

    The ORDER below is the contract, not the individual pieces:

    * the request id must exist before anything logs or answers with it
    * the security headers must be set before a handler can write a body
    * the recoverer must sit inside the logger, so a failing request is logged
      as a completed 500 rather than vanishing
    * CORS must sit inside the logger, so a preflight that short-circuits is
      still logged, and outside the recoverer, so a 500 carries CORS headers
    * the rate limiter must sit inside all of them, so its refusal is a fully
      formed answer rather than a bare 429

    Starlette applies middleware in reverse registration order, so the calls
    below read outermost-last.

    ``trusted_prefixes`` names path prefixes that are NOT BROWSER SURFACES: an
    operator or fleet surface reached by another server holding a credential. One
    parameter drives two exemptions because it is one fact about the service, and
    splitting it across ``CORS`` and ``RateLimit`` would let a deployment exempt
    a prefix from one and forget the other.

    * CORS is skipped, because ``access-control-allow-origin`` on those paths
      names a browser origin that will never call them. See
      ``ScopedCORSMiddleware`` for what that does and, more importantly, does not
      achieve: it removes a false statement, it does not hide a route.
    * The limiter is skipped, for the reason ``EXEMPT_PATHS`` already gives about
      the probes. A per-address bucket is the wrong instrument for a surface
      whose every caller shares one trusted address; the credential is what
      guards it, and a 429 there refuses an operator rather than an abuser.

    It is deliberately NOT authorization and grants nothing. A prefix listed here
    is exactly as reachable as it was, and whatever guards its router still does.
    """
    log = logger or logging.getLogger("kit.httpapi")
    # Checked before a single middleware is added, so a service configured
    # into "exempt everything" fails to start rather than serving wide open.
    trusted_prefixes = normalize_trusted_prefixes(trusted_prefixes)

    # Innermost first, because Starlette wraps in reverse.
    if rate_limit is not None:
        app.add_middleware(
            RateLimitMiddleware,
            limit=rate_limit,
            logger=log,
            exempt_prefixes=trusted_prefixes,
        )

    app.add_middleware(RecoveryMiddleware, logger=log)

    if cors is not None and cors.enabled:
        if cors.public_read:
            app.add_middleware(
                ScopedCORSMiddleware,
                prefixes=trusted_prefixes,
                options=_options(
                    allow_origins=["*"],
                    # Never credentials. Its absence is what makes the wildcard both
                    # browser-legal and safe.
                    allow_credentials=False,
                    allow_methods=[m.strip() for m in PUBLIC_READ_METHODS.split(",")],
                    allow_headers=[h.strip() for h in PUBLIC_READ_HEADERS.split(",")],
                    expose_headers=[h.strip() for h in EXPOSED_HEADERS.split(",")],
                    max_age=MAX_AGE_SECONDS,
                ),
            )
        else:
            app.add_middleware(
                ScopedCORSMiddleware,
                prefixes=trusted_prefixes,
                options=_options(
                    allow_origins=sorted(normalize_origins(cors.allowed_origins)),
                    allow_credentials=True,
                    allow_methods=[m.strip() for m in CREDENTIALED_METHODS.split(",")],
                    allow_headers=[h.strip() for h in CREDENTIALED_HEADERS.split(",")],
                    expose_headers=[h.strip() for h in EXPOSED_HEADERS.split(",")],
                    max_age=MAX_AGE_SECONDS,
                ),
            )

    app.add_middleware(RequestLogMiddleware, logger=log)
    app.add_middleware(RequestIDMiddleware)

    install_error_handlers(app, code_for=code_for)
    app.include_router(probe_router(readiness, logger=log))
