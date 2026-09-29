"""Shared helpers for scrubbing bytes that a downstream sink can't accept."""

from __future__ import annotations

from typing import Any


def strip_nul_bytes(value: Any) -> Any:
    """Recursively strip embedded NUL bytes from a string, or from dict/list/tuple/set values.

    Dict keys are sanitized too, not only values: JSONB rejects an embedded NUL in a key
    exactly as in a value.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {strip_nul_bytes(k): strip_nul_bytes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_nul_bytes(v) for v in value]
    if isinstance(value, tuple):
        return tuple(strip_nul_bytes(v) for v in value)
    if isinstance(value, set):
        return {strip_nul_bytes(v) for v in value}
    return value
