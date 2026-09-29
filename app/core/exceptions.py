"""Application exception types and FastAPI handlers for consistent HTTP error responses."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import ClassVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.envelope import error_envelope
from app.core.logging import REQUEST_ID_HEADER, get_request_id

logger = logging.getLogger(__name__)

GENERIC_500_MESSAGE = "Internal server error"


class AppError(Exception):
    """Base for every expected application error.

    Subclasses fix `status_code`; `code`, `message` and `details` come from the
    raise site. Only `message` reaches the response body, and `details` is
    exposed on 4xx responses but withheld on 5xx ones.
    """

    status_code: ClassVar[int]

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message if details is None else f"{message} :: {details}")
        self.code = code
        self.message = message
        self.details = details


class NotFoundError(AppError):
    """A requested resource does not exist."""

    status_code: ClassVar[int] = 404


class ConflictError(AppError):
    """A duplicate resource or a concurrent write blocks the request."""

    status_code: ClassVar[int] = 409


class ValidationError(AppError):
    """Request data is well-formed but semantically invalid."""

    status_code: ClassVar[int] = 422


class BusinessRuleError(AppError):
    """A domain rule or expected-state precondition fails."""

    status_code: ClassVar[int] = 409


class ExternalServiceError(AppError):
    """An upstream dependency failed unexpectedly."""

    status_code: ClassVar[int] = 502


class NotAuthenticatedError(AppError):
    """The caller presented no valid internal API key."""

    status_code: ClassVar[int] = 401


def _resolve_request_id(request: Request) -> str:
    """Resolve the request ID from request state, then the request context."""
    return getattr(request.state, "request_id", None) or get_request_id()


def _is_json_safe(value: object) -> bool:
    """Whether `value` survives `json.dumps` unchanged (recursively)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_safe(item) for key, item in value.items())
    return False


def _sanitize_validation_errors(errors: Sequence[dict]) -> list[dict]:
    """Make every error dict from `RequestValidationError.errors()` JSON-safe.

    Pydantic v2 puts the raw `ValueError` from a custom validator into the
    error dict's `ctx` key. `JSONResponse.render` has no fallback encoder, so
    that object turns the intended 422 into an unhandled `TypeError`.
    Stringify any non-JSON-safe `ctx` value rather than dropping it, keeping
    the validator's own message visible to the client.
    """
    sanitized = []
    for error in errors:
        ctx = error.get("ctx")
        if isinstance(ctx, dict):
            error = {
                **error,
                "ctx": {key: value if _is_json_safe(value) else str(value) for key, value in ctx.items()},
            }
        sanitized.append(error)
    return sanitized


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers for expected application and unexpected exceptions.

    Every path below (`AppError`, FastAPI's native `RequestValidationError`, a
    bare `HTTPException`, and the unhandled-exception catch-all) produces the
    same `{success, message, data, error}` envelope, with `data` always `None`.
    """

    @app.exception_handler(AppError)
    async def _app_error_handler(
        request: Request,
        exc: AppError,
    ) -> JSONResponse:
        """Log the raised AppError and translate it into an envelope-shaped JSON response."""
        request_id = _resolve_request_id(request)
        is_server_error = exc.status_code >= 500

        logger.log(
            logging.ERROR if is_server_error else logging.WARNING,
            "%s %s -> %s %s: %s%s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.code,
            exc.message,
            f" | details={exc.details}" if exc.details else "",
            exc_info=exc if is_server_error else None,
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": exc.status_code,
                "error_code": exc.code,
            },
        )

        # `details` may contain client-facing validation information for 4xx
        # errors. Do not expose it for 5xx errors, since call sites may put
        # internal failure information there.
        envelope = error_envelope(
            exc.code,
            exc.message,
            details=exc.details if not is_server_error else None,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(),
            headers={REQUEST_ID_HEADER: request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Log and translate FastAPI's RequestValidationError into a 422 envelope response."""
        request_id = _resolve_request_id(request)

        logger.warning(
            "%s %s -> 422 REQUEST_VALIDATION_ERROR: %s",
            request.method,
            request.url.path,
            exc.errors(),
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": 422,
                "error_code": "REQUEST_VALIDATION_ERROR",
            },
        )

        envelope = error_envelope(
            "REQUEST_VALIDATION_ERROR",
            "Request validation failed.",
            details={"errors": _sanitize_validation_errors(exc.errors())},
        )
        return JSONResponse(
            status_code=422,
            content=envelope.model_dump(),
            headers={REQUEST_ID_HEADER: request_id},
        )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request: Request,
        exc: HTTPException,
    ) -> JSONResponse:
        """Reshape a bare FastAPI/Starlette HTTPException into the same envelope shape as AppError."""
        request_id = _resolve_request_id(request)
        is_server_error = exc.status_code >= 500

        logger.log(
            logging.ERROR if is_server_error else logging.WARNING,
            "%s %s -> %s HTTP_%s: %s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.status_code,
            exc.detail,
            exc_info=exc if is_server_error else None,
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": exc.status_code,
                "error_code": f"HTTP_{exc.status_code}",
            },
        )

        envelope = error_envelope(f"HTTP_{exc.status_code}", str(exc.detail))
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(),
            headers={REQUEST_ID_HEADER: request_id, **(exc.headers or {})},
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Log an unhandled exception and return a generic 500 envelope, hiding internal detail."""
        request_id = _resolve_request_id(request)

        logger.error(
            "%s %s -> 500 unhandled %s",
            request.method,
            request.url.path,
            type(exc).__name__,
            exc_info=exc,
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": 500,
                "error_code": "INTERNAL_ERROR",
            },
        )

        envelope = error_envelope("INTERNAL_ERROR", GENERIC_500_MESSAGE)
        return JSONResponse(
            status_code=500,
            content=envelope.model_dump(),
            headers={REQUEST_ID_HEADER: request_id},
        )
