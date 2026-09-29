"""Merges a proposed CMIR draft onto the active record."""

from __future__ import annotations

from typing import Any

from app.schemas.cmir import CMIR_CONTENT_FIELDS, Cmir


def merge_with_active(
    existing: dict[str, Any] | None, proposed: Cmir
) -> tuple[Cmir, dict[str, dict[str, Any]]]:
    """Merge proposed CMIR onto active record; nonblank proposed values win.

    Returns merged CMIR plus diff of changed fields.
    """
    existing = existing or {}
    merged_values: dict[str, str] = {}
    diff: dict[str, dict[str, Any]] = {}

    for field_name in CMIR_CONTENT_FIELDS:
        proposed_value = str(getattr(proposed, field_name, "") or "")
        existing_value = str(existing.get(field_name) or "")
        merged_value = proposed_value if proposed_value.strip() else existing_value

        merged_values[field_name] = merged_value
        if merged_value != existing_value:
            diff[field_name] = {"from": existing_value, "to": merged_value}

    merged = Cmir.model_validate(merged_values)
    return merged, diff
