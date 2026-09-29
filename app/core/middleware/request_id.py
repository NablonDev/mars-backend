"""ASGI middleware for request ID propagation and response correlation."""

from __future__ import annotations

import re
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import REQUEST_ID_HEADER, reset_request_id, set_request_id

_SAFE_REQUEST_ID = re.compile(r"\A[A-Za-z0-9.*:-]{1,64}\Z")


def _inbound_request_id(scope: Scope) -> str | None:
    """Return the inbound X-Request-ID header value if present and safe to reuse, else None."""
    candidate = Headers(scope=scope).get(REQUEST_ID_HEADER)
    if candidate and _SAFE_REQUEST_ID.fullmatch(candidate):
        return candidate
    return None


class RequestIdMiddleware:
    """ASGI middleware that assigns a request ID and echoes it back in the response header."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _inbound_request_id(scope) or uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        token = set_request_id(request_id)

        async def send_with_request_id(message: Message) -> None:
            """Forward the ASGI message, stamping the response start with the request ID header."""
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_id(token)
