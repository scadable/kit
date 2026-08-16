"""Assemble the chain. This is where the order lives."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

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
from kit.httpapi._handlers import CodeMapper, default_code_for, install_error_handlers
from kit.httpapi._middleware import (
    RecoveryMiddleware,
    RequestIDMiddleware,
    RequestLogMiddleware,
)
from kit.httpapi._probes import probe_router
from kit.httpapi._ratelimit import RateLimit
from kit.httpapi._ratelimit_middleware import RateLimitMiddleware


def install_conventions(
    app: FastAPI,
    *,
    readiness: Registry,
    cors: CORS | None = None,
    rate_limit: RateLimit | None = None,
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
    """
    log = logger or logging.getLogger("kit.httpapi")

    # Innermost first, because Starlette wraps in reverse.
    if rate_limit is not None:
        app.add_middleware(RateLimitMiddleware, limit=rate_limit, logger=log)

    app.add_middleware(RecoveryMiddleware, logger=log)

    if cors is not None and cors.enabled:
        if cors.public_read:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                # Never credentials. Its absence is what makes the wildcard both
                # browser-legal and safe.
                allow_credentials=False,
                allow_methods=[m.strip() for m in PUBLIC_READ_METHODS.split(",")],
                allow_headers=[h.strip() for h in PUBLIC_READ_HEADERS.split(",")],
                expose_headers=[h.strip() for h in EXPOSED_HEADERS.split(",")],
                max_age=MAX_AGE_SECONDS,
            )
        else:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=sorted(normalize_origins(cors.allowed_origins)),
                allow_credentials=True,
                allow_methods=[m.strip() for m in CREDENTIALED_METHODS.split(",")],
                allow_headers=[h.strip() for h in CREDENTIALED_HEADERS.split(",")],
                expose_headers=[h.strip() for h in EXPOSED_HEADERS.split(",")],
                max_age=MAX_AGE_SECONDS,
            )

    app.add_middleware(RequestLogMiddleware, logger=log)
    app.add_middleware(RequestIDMiddleware)

    install_error_handlers(app, code_for=code_for)
    app.include_router(probe_router(readiness, logger=log))
