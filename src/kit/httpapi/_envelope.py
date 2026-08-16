"""The error envelope. Import from ``kit.httpapi``."""

from __future__ import annotations

from contextvars import ContextVar

from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

# Set by the request-id middleware, read by anything that builds an envelope.
# A ContextVar rather than a request attribute so a service can log the id from
# code that was never handed the request.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
"""Package-internal: set by the request-id middleware, read by the envelope."""
_request_id = request_id_var

JSON_CONTENT_TYPE = "application/json; charset=utf-8"


def request_id() -> str:
    """The id assigned to the request in flight, or empty outside one."""
    return _request_id.get()


class ErrorDetail(BaseModel):
    """The machine-readable code and the human-readable message.

    The message is written for the caller and must never carry internal detail.
    """

    code: str = Field(examples=["not_found"])
    message: str = Field(examples=["No policy with that id"])


class ErrorEnvelope(BaseModel):
    """The error shape every service returns, request id included.

    A caller reporting a failure can then name the exact log line. There are no
    other top-level keys, and in particular not ``detail``, which is what
    FastAPI emits by default and which every service must override.
    """

    error: ErrorDetail
    request_id: str = Field(examples=["01JC8Z2Q7X8V3K1M4N5P6R7S8T"])


class StatusResponse(BaseModel):
    """The one-word verdict used by liveness, and by anything whose whole answer
    is that it worked."""

    status: str = Field(examples=["ok"])


# The vocabulary. Codes are snake_case, lowercase and non-hierarchical. Clients
# branch on them, so renaming one is a breaking change and belongs to a version.
INVALID_REQUEST = "invalid_request"
UNAUTHENTICATED = "unauthenticated"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
CONFLICT = "conflict"
ALREADY_EXISTS = "already_exists"
RATE_LIMITED = "rate_limited"
UPSTREAM_UNAVAILABLE = "upstream_unavailable"
NOT_CONFIGURED = "not_configured"
INTERNAL_ERROR = "internal_error"


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Build the standard error envelope as a response."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"code": code, "message": message},
            "request_id": request_id(),
        },
        media_type=JSON_CONTENT_TYPE,
    )
