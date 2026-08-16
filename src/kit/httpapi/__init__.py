"""The HTTP conventions every service shares.

Request id generation and the ``X-Request-ID`` response header, the request
logger that records the route PATTERN and never the URL, the error envelope
``{"error": {"code", "message"}, "request_id"}``, the ``/healthz`` and
``/readyz`` handlers, CORS, and the in-process rate limiter.

This package owns how a response looks. It does not own routing, and it never
knows what any particular service exposes. That is deliberate: which routes exist
and what they disclose is exactly the decision that must not be made once and
inherited.
"""

from kit.httpapi._cors import CORS, normalize_origins
from kit.httpapi._envelope import (
    ALREADY_EXISTS,
    CONFLICT,
    FORBIDDEN,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    NOT_CONFIGURED,
    NOT_FOUND,
    RATE_LIMITED,
    UNAUTHENTICATED,
    UPSTREAM_UNAVAILABLE,
    ErrorDetail,
    ErrorEnvelope,
    StatusResponse,
    error_response,
    request_id,
)
from kit.httpapi._handlers import (
    default_code_for,
    install_error_handlers,
    json_error,
)
from kit.httpapi._install import install_conventions
from kit.httpapi._middleware import (
    REQUEST_ID_HEADER,
    RecoveryMiddleware,
    RequestIDMiddleware,
    RequestLogMiddleware,
)
from kit.httpapi._probes import ReadinessResponse, probe_router
from kit.httpapi._ratelimit import DEFAULT_RATE_LIMIT, RateLimit, unlimited
from kit.httpapi._ratelimit_middleware import RateLimitMiddleware

__all__ = [
    "ALREADY_EXISTS",
    "CONFLICT",
    "CORS",
    "DEFAULT_RATE_LIMIT",
    "FORBIDDEN",
    "INTERNAL_ERROR",
    "INVALID_REQUEST",
    "NOT_CONFIGURED",
    "NOT_FOUND",
    "RATE_LIMITED",
    "REQUEST_ID_HEADER",
    "UNAUTHENTICATED",
    "UPSTREAM_UNAVAILABLE",
    "ErrorDetail",
    "ErrorEnvelope",
    "RateLimit",
    "RateLimitMiddleware",
    "ReadinessResponse",
    "RecoveryMiddleware",
    "RequestIDMiddleware",
    "RequestLogMiddleware",
    "StatusResponse",
    "default_code_for",
    "error_response",
    "install_conventions",
    "install_error_handlers",
    "json_error",
    "normalize_origins",
    "probe_router",
    "request_id",
    "unlimited",
]
