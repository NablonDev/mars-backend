"""Shared short-id generator for prefixed correlation keys (checkpoint threads, batches)."""

from __future__ import annotations

from uuid import uuid4


def new_id(prefix: str) -> str:
    """Generate a fresh, unique id of the form `{prefix}_{12 hex chars}`."""
    return f"{prefix}_{uuid4().hex[:12]}"
