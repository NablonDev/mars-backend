"""Workflow execution state for the CMIR resolution pipeline.

Carries extracted email data, validation results, and merge decisions through
the graph nodes. Updated by each node as it processes the email message.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict
from uuid import UUID


class GraphState(TypedDict, total=False):
    """Workflow execution state.

    Holds all data flowing through the CMIR extraction, validation, and merge
    pipeline. Each node reads from and updates this state as the email moves
    from receipt through AI extraction, human approval, and persistence.
    """

    email: dict[str, Any]  # EmailMessage, as a dict
    email_id: Any
    batch_id: str
    run_id: UUID
    thread_id: str
    cmir: dict[str, Any]  # CMIR, as a dict
    decision: str | None  # "approve" | "reject"
    decision_reason: str
    existing_cmir: dict[str, Any] | None  # current cmir_records row for this entity, or None
    cmir_diff: dict[str, Any]  # field-level diff from merge_with_active
    cmir_version_token: UUID | None  # existing_cmir["id"] at diff time, for supersede_and_insert
    cmir_write_result: Literal["committed", "conflict"]  # set by persist_cmir
