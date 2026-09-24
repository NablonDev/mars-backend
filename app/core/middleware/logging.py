"""ASGI middleware for structured HTTP access logging."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("app.access")

_MAX_BODY_BUFFER_BYTES = 512 * 1024  # 512 KB
_COLLECTION_KEYS = ("items", "lines", "scenarios", "records")


def _summarize_data(data: Any) -> str:
    """Summarize payload or envelope data into 'empty', '1 item', or 'N items'."""
    if data is None:
        return "empty"

    if isinstance(data, bool):
        return "1 item"

    if isinstance(data, (int, float)):
        return "1 item"

    if isinstance(data, str):
        return "empty" if not data.strip() else "1 item"

    if isinstance(data, list):
        count = len(data)
        if count == 0:
            return "empty"
        if count == 1:
            return "1 item"
        return f"{count} items"

    if isinstance(data, dict):
        if not data:
            return "empty"

        for key in _COLLECTION_KEYS:
            val = data.get(key)
            if isinstance(val, list):
                count = len(val)
                if count == 0:
                    return "empty"
                if count == 1:
                    return "1 item"
                return f"{count} items"

        if len(data) == 1:
            only_val = next(iter(data.values()))
            if isinstance(only_val, list):
                count = len(only_val)
                if count == 0:
                    return "empty"
                if count == 1:
                    return "1 item"
                return f"{count} items"

        return "1 item"

    return "1 item"


def determine_response_badge(
    status: int,
    is_json: bool,
    body: bytes,
) -> tuple[str | None, str | None]:
    """Inspect response status and body to determine an envelope-aware badge and error code.

    Returns:
        (response_badge, error_code)
        response_badge: "empty", "1 item", "N items", or None.
        error_code: extracted error code string (for status >= 400 or failed envelope) or None.
    """
    if status >= 400:
        error_code: str | None = None
        if is_json and body:
            try:
                payload = json.loads(body.decode("utf-8"))
                if isinstance(payload, dict):
                    err = payload.get("error")
                    if isinstance(err, dict) and "code" in err:
                        error_code = str(err["code"])
                    elif "code" in payload:
                        error_code = str(payload["code"])
            except (json.JSONDecodeError, UnicodeDecodeError):
                error_code = None
        return None, error_code

    # 3xx redirects / cache hits have no data body to evaluate
    if 300 <= status < 400:
        return None, None

    # 204 No Content or zero-byte body
    if status == 204 or len(body) == 0:
        return "empty", None

    if not is_json:
        return None, None

    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None

    # Envelope-aware extraction
    if isinstance(payload, dict) and "success" in payload:
        if payload.get("success") is False:
            err = payload.get("error")
            err_code = str(err["code"]) if isinstance(err, dict) and "code" in err else None
            return None, err_code

        if "data" in payload:
            return _summarize_data(payload["data"]), None

    # Plain JSON payload (non-envelope)
    return _summarize_data(payload), None


class AccessLogMiddleware:
    """ASGI middleware that logs one line per HTTP request with method, path, status, and duration."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500
        logged = False
        body_chunks: list[bytes] = []
        is_json = False
        total_body_bytes = 0

        def log_once() -> None:
            """Emit the access-log line once, guarding against the send hook and the finally block both firing."""
            nonlocal logged

            if logged:
                return

            logged = True
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            method = scope.get("method", "-")
            path = scope.get("path", "-")

            body_bytes = b"".join(body_chunks)
            response_badge, error_code = determine_response_badge(
                status=status,
                is_json=is_json,
                body=body_bytes,
            )

            extra: dict[str, object] = {
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": duration_ms,
            }
            if response_badge is not None:
                extra["response_badge"] = response_badge
            if error_code is not None:
                extra["error_code"] = error_code

            msg = "%s %s %s %sms"
            msg_args: list[object] = [method, path, status, duration_ms]
            if response_badge:
                msg += " [%s]"
                msg_args.append(response_badge)

            logger.info(msg, *msg_args, extra=extra)

        async def send_with_status(message: Message) -> None:
            """Capture the response status, forward the message, then log once the body is fully sent."""
            nonlocal status, is_json, total_body_bytes

            if message["type"] == "http.response.start":
                status = message["status"]
                headers = message.get("headers", [])
                for raw_name, raw_val in headers:
                    if raw_name.lower() == b"content-type" and b"application/json" in raw_val.lower():
                        is_json = True

            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk and total_body_bytes < _MAX_BODY_BUFFER_BYTES:
                    body_chunks.append(chunk)
                total_body_bytes += len(chunk)

            await send(message)

            # Log when the response body is actually sent, rather than when
            # the ASGI application returns (which can include background work).
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                log_once()

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            # Covers requests that fail before a response body is sent.
            log_once()
