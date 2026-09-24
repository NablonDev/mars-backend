"""API schemas for the shared `process.workflow_thread` surface (`app/api/v1/workflow_threads.py`).

Shapes here are domain-agnostic: both the `cmir` and `po_validation` routers use them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, PlainSerializer

# ---------------------------------------------------------------------------
# Shared thread stage / snapshot shapes
# ---------------------------------------------------------------------------

# The service layer compares `expected_updated_at` against `datetime.isoformat()`, which renders
# UTC as `+00:00`; Pydantic's default JSON serialization renders it as `Z` and fails that exact
# string comparison. Serializing via `.isoformat()` keeps `updated_at` round-trippable verbatim.
IsoDatetime = Annotated[datetime, PlainSerializer(lambda value: value.isoformat(), return_type=str)]


class WorkflowThreadResponse(BaseModel):
    """Response shape for a `process.workflow_thread` row, shared across both domains."""

    id: UUID
    job_item_id: UUID | None = None
    status: str
    stage: str
    current_node: str | None = None
    completed_at: datetime | None = None
    error: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    email_event_id: UUID | None = None
    purchase_order_line_id: UUID | None = None
    updated_at: IsoDatetime


class WorkflowThreadListResponse(BaseModel):
    """Paginated response shape for `GET /workflow-threads`."""

    items: list[WorkflowThreadResponse]
    next_cursor: str | None = None


class WorkflowThreadDetailResponse(WorkflowThreadResponse):
    """Response shape for `GET /workflow-threads/{thread_id}`, with an opt-in untyped `snapshot`."""

    snapshot: dict[str, Any] | None = None


class SnapshotHistoryItem(BaseModel):
    """One `human_action` row for a thread, which may still be open and therefore unanswered."""

    actor: str | None = None
    action_type: str | None = None
    decision: str | None = None
    response_payload: dict[str, Any] | None = None
    responded_at: datetime | None = None


class CmirThreadSnapshotResponse(WorkflowThreadResponse):
    """CMIR-domain snapshot shape: the shared thread stage plus its `human_action` history."""

    history: list[SnapshotHistoryItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# POST /workflow-threads/{thread_id}/missing-fields,
# PATCH /workflow-threads/{thread_id}/draft
# ---------------------------------------------------------------------------


class WorkflowThreadFieldsRequest(BaseModel):
    """Field edits for `POST .../missing-fields` and `PATCH .../draft`, guarded by `updated_at`."""

    actor: str
    fields: dict[str, Any]
    expected_updated_at: str


class WorkflowThreadDraftResponse(BaseModel):
    """Response shape for a workflow-thread draft update."""

    agent_run_id: UUID
    thread_id: str
    stage: str
    status: str
    pending_action_id: UUID
    message: str


# ---------------------------------------------------------------------------
# POST /workflow-threads/{thread_id}/decisions: one endpoint, discriminated on `decision_type`.
# ---------------------------------------------------------------------------


class CmirApprovalDecisionRequest(BaseModel):
    """Approve or reject a proposed CMIR record."""

    decision_type: Literal["CMIR_APPROVAL"] = "CMIR_APPROVAL"
    actor: str
    decision: Literal["approve", "reject"]
    expected_updated_at: str
    reason: str = ""


class QtyMismatchDecisionRequest(BaseModel):
    """Resolve a quantity mismatch on a PO line."""

    decision_type: Literal["QTY_MISMATCH"] = "QTY_MISMATCH"
    actor: str
    decision: Literal["use_substitute", "proceed_anyway", "mark_stale"]
    substitute_material_code: str | None = None
    expected_updated_at: str


class ManualCmirEntryDecisionRequest(BaseModel):
    """Supply a CMIR mapping by hand when extraction could not resolve one."""

    decision_type: Literal["MANUAL_CMIR_ENTRY"] = "MANUAL_CMIR_ENTRY"
    actor: str
    sap_material_number: str
    description: str = ""
    expected_updated_at: str


WorkflowThreadDecisionRequest = Annotated[
    CmirApprovalDecisionRequest | QtyMismatchDecisionRequest | ManualCmirEntryDecisionRequest,
    Field(discriminator="decision_type"),
]
