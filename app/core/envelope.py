"""Unified `{success, message, data, error}` API response envelope."""

from __future__ import annotations

from pydantic import BaseModel


class ErrorBody(BaseModel):
    """Error code and optional details carried in a failed Envelope response."""

    code: str
    details: str | dict | None = None


class Envelope[T](BaseModel):
    """Generic `{success, message, data, error}` wrapper for every API response."""

    success: bool
    message: str
    data: T | None = None
    error: ErrorBody | None = None


def success_envelope[T](data: T, message: str = "OK") -> Envelope[T]:
    """Build a successful Envelope wrapping data."""
    return Envelope[T](success=True, message=message, data=data, error=None)


def error_envelope(code: str, message: str, details: str | dict | None = None) -> Envelope[None]:
    """Build a failed Envelope carrying an ErrorBody."""
    return Envelope[None](
        success=False, message=message, data=None, error=ErrorBody(code=code, details=details)
    )
