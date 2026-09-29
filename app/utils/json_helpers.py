"""Shared JSON serialization helpers for wrapping untrusted content handed to an LLM."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any


def json_default(value: Any) -> Any:
    """Serialize a date as ISO 8601; fall back to str() for anything else json.dumps can't handle."""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def wrap_data(payload: dict | list, default: Callable[[Any], Any] | None = None) -> str:
    """Serialize payload and wrap it in `<DATA>` tags marking it as untrusted, non-instructional content."""
    body = json.dumps(payload, default=default, sort_keys=True)
    return f"<DATA>\n{body}\n</DATA>"
