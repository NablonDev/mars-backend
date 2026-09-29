"""Shared clock helpers, so no call site re-derives its own `datetime.now(...)`."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings


def utc_now() -> datetime:
    """Current time, timezone-aware UTC."""
    return datetime.now(UTC)


def utc_today() -> date:
    """Current calendar date in UTC."""
    return utc_now().date()


def utc_now_naive() -> datetime:
    """Current UTC time with tzinfo stripped, for columns that expect a naive value."""
    return utc_now().replace(tzinfo=None)


def business_today() -> date:
    """Today in the configured business timezone, the same clock the workers bill against."""
    return datetime.now(ZoneInfo(get_settings().summary.business_timezone)).date()
