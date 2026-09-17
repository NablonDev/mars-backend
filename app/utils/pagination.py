"""Shared helpers for cursor-based pagination (CMIR/PO Validation list endpoints)."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_cursor(cursor: str | None) -> datetime | None:
    """Parse an ISO 8601 `updated_at` cursor into a datetime; raises ValueError if malformed."""
    if cursor is None:
        return None
    return datetime.fromisoformat(cursor)


def next_cursor_from_page(items: list[dict[str, Any]], limit: int, key: str = "updated_at") -> str | None:
    """Derive the next-page cursor from a full page's last item, or `None` if the page was short."""
    return items[-1][key].isoformat() if len(items) == limit and items else None
