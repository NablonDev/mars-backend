"""Shared best-effort heartbeat invocation for long-running LLM agent tool-calling loops."""

from __future__ import annotations

import logging
from collections.abc import Callable


def invoke_heartbeat(heartbeat: Callable[[], None] | None, context_id: str, logger: logging.Logger) -> None:
    """Best-effort heartbeat; callback failures never abort generation."""
    if heartbeat is None:
        return
    try:
        heartbeat()
    except Exception:
        logger.exception("Heartbeat callback failed for context_id=%s; continuing generation", context_id)
