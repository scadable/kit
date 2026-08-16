"""Exception handlers that put every failure in the house envelope."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kit.httpapi._envelope import (
    CONFLICT,
    FORBIDDEN,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    NOT_FOUND,
    RATE_LIMITED,
    UNAUTHENTICATED,
    error_response,
)

# Which code a status maps to when the raiser did not say. A status with no
# entry becomes internal_error, which is the fail-closed direction.
_CODE_BY_STATUS = {
    400: INVALID_REQUEST,
    401: UNAUTHENTICATED,
    403: FORBIDDEN,
    404: NOT_FOUND,
    405: "method_not_allowed",
    409: CONFLICT,
    422: INVALID_REQUEST,
    429: RATE_LIMITED,
}

CodeMapper = Callable[[int], str]


def default_code_for(status_code: int) -> str:
    return _CODE_BY_STATUS.get(status_code, INTERNAL_ERROR)


def install_error_handlers(
    app: FastAPI,
    *,
    code_for: CodeMapper = default_code_for,
) -> None:
    """Make every error answer in the house envelope.

    ``code_for`` is policy and yours to override. The envelope SHAPE is contract
    and is not overridable: changing it does not customise your service, it
    desynchronises it from every dashboard and client in the fleet.
    """

    async def on_validation_error(_request: Request, exc: RequestValidationError) -> Response:
        # 422 is declared on routes explicitly for this reason: FastAPI injects
        # its own validation shape only when no 422 is already documented, so
        # without that declaration a service documents a shape it does not send.
        del exc
        return error_response(422, INVALID_REQUEST, "The request body or parameters are not valid.")

    async def on_http_error(_request: Request, exc: StarletteHTTPException) -> Response:
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail else ""  # pyright: ignore[reportUnnecessaryIsInstance]
        return error_response(
            exc.status_code,
            code_for(exc.status_code),
            detail or "The request could not be completed.",
        )

    async def on_unhandled(_request: Request, exc: Exception) -> Response:
        del exc
        return error_response(500, INTERNAL_ERROR, "An internal error occurred.")

    # StarletteHTTPException, not FastAPI's subclass. Registering the subclass
    # leaves unrouted paths and 405s answering Starlette's {"detail": ...},
    # because the router raises the base class before FastAPI is involved.
    app.add_exception_handler(StarletteHTTPException, on_http_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, on_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, on_unhandled)


def json_error(status_code: int, code: str, message: str) -> JSONResponse:
    """Build one envelope directly, for a handler that wants to return rather
    than raise."""
    return error_response(status_code, code, message)
