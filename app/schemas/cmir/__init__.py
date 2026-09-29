"""CMIR-domain schemas; re-exports the graph-state and value-object shapes from `domain`."""

from __future__ import annotations

from app.schemas.cmir.domain import (
    CMIR_CONTENT_FIELDS,
    MANDATORY_FIELDS,
    Cmir,
    CmirStatus,
    EmailMessage,
)

__all__ = [
    "CMIR_CONTENT_FIELDS",
    "MANDATORY_FIELDS",
    "Cmir",
    "CmirStatus",
    "EmailMessage",
]
