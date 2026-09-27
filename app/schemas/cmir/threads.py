"""API schemas for the shared `process.workflow_thread` surface -- used by
both the `cmir` and `po_validation` routers (`app/api/v1/workflow_threads.py`),
per the approved plan §6: `workflow_thread` is a `process`-schema resource
now, not a cmir-only one, so its request/decision shapes live here rather
than being duplicated per domain. `app/schemas/po_validation/threads.py`
imports `SnapshotHistoryItem` from this module, continuing the
already-established cross-import (the pre-Phase-7b `app/schemas/po_validation.py`
did the same).

`WorkflowThreadResponse` mirrors `WorkflowThreadRepository`'s shared dict
shape (`_thread_to_dict`) verbatim -- it is genuinely domain-agnostic:
identical for a cmir-domain thread (`email_event_id` set) or a
po_validation-domain thread (`purchase_order_line_id` set), since both
`CmirRunService.get_stage`/`PoValidationService.get_stage` delegate to the
same repository method.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, PlainSerializer

# ---------------------------------------------------------------------------
# Shared thread stage / snapshot shapes
# ---------------------------------------------------------------------------

# `POST .../missing-fields`/`draft`/`decisions` all take an
# `expected_updated_at` string that must match the thread's current
# `updated_at` *exactly* -- the service layer compares it against
# `datetime.isoformat()` (`app.services.cmir.run_service.CmirRunService.
# _ensure_current`), which renders a UTC offset as `+00:00`. Pydantic's
# default JSON-mode datetime serialization renders UTC as a `Z` suffix
# instead (`2026-01-01T00:00:00Z` vs `...+00:00`) -- two representations of
# the same instant that fail this route's own exact-string comparison. This
# `PlainSerializer` makes the wire format match `.isoformat()` byte-for-byte,
# so a client can round-trip `updated_at` -> `expected_updated_at` verbatim.
IsoDatetime = Annotated[datetime, PlainSerializer(lambda value: value.isoformat(), return_type=str)]


class WorkflowThreadResponse(BaseModel):
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
    # List-view-only business context (2026-09-15) -- populated by
    # `WorkflowThreadRepository.list_threads` only (one batched query for the
    # whole page for the PO-validation fields; read off the thread's own
    # `metadata_json.latest_snapshot.cmir` for CMIR, no query at all), so the
    # Error Queue can show real customer/PO/material context per row instead
    # of only a UUID. Always None on `get_by_id`/`get_snapshot` responses,
    # which don't populate them -- the full snapshot is the source of truth
    # for a selected thread's detail; this is only a cheap list-level summary.
    po_number: str | None = None
    po_line_number: str | None = None
    retailer_material_code: str | None = None
    order_quantity: float | None = None
    retailer_name: str | None = None
    customer_identity: str | None = None
    material_identity: str | None = None


class WorkflowThreadListResponse(BaseModel):
    items: list[WorkflowThreadResponse]
    next_cursor: str | None = None


class WorkflowThreadDetailResponse(WorkflowThreadResponse):
    """`GET /workflow-threads/{thread_id}?include=snapshot` -- stage is
    always returned (the base `WorkflowThreadResponse` fields), `snapshot`
    is opt-in and untyped: CMIR-domain and po_validation-domain snapshots
    have different shapes (`CmirThreadSnapshotResponse` vs
    `app.schemas.po_validation.threads.PoValidationThreadSnapshotResponse`),
    so this route has no single fixed shape for it -- see
    `app/api/v1/workflow_threads.py`."""

    snapshot: dict[str, Any] | None = None


class SnapshotHistoryItem(BaseModel):
    """One `human_action` row for a thread. `WorkflowThreadRepository.get_snapshot`'s
    query returns every row for the thread, including the still-open one (if
    any) -- `actor`/`response_payload`/`responded_at` are only set once a row
    is completed (see `HumanActionRepository.apply_human_action`/`complete`),
    so all three must tolerate `None` here."""

    actor: str | None = None
    action_type: str | None = None
    decision: str | None = None
    response_payload: dict[str, Any] | None = None
    responded_at: datetime | None = None


class CmirThreadSnapshotResponse(WorkflowThreadResponse):
    """CMIR-domain snapshot shape -- `WorkflowThreadRepository.get_snapshot`'s
    real return value (`CmirRunService.get_snapshot` adds `editable_fields`
    to it): the shared thread-stage dict plus its `human_action` history.
    This is thinner than the pre-restructure PRD §10.4 shape (no top-level
    `cmir`/`existing_cmir`/`diff`/`email` -- that detail stays nested inside
    `metadata_json["latest_snapshot"]`, shaped differently per interrupt
    type, so it is not flattened here). `editable_fields` is the one
    exception: it's the fixed `CMIR_CONTENT_FIELDS` set, not volatile
    per-stage data like `cmir`/`existing_cmir`/`diff`, so flattening it onto
    this model doesn't defeat the reason those stay nested. Structurally
    different from `app.schemas.po_validation.threads.PoValidationThreadSnapshotResponse`
    (PRD §11.3), so `GET /workflow-threads/{thread_id}?include=snapshot`
    types its `snapshot` field as `dict[str, Any]` rather than a single fixed
    model -- see `app/api/v1/workflow_threads.py`."""

    history: list[SnapshotHistoryItem] = Field(default_factory=list)
    editable_fields: list[str]


# ---------------------------------------------------------------------------
# POST /workflow-threads/{thread_id}/missing-fields,
# PATCH /workflow-threads/{thread_id}/draft
# ---------------------------------------------------------------------------


class WorkflowThreadFieldsRequest(BaseModel):
    actor: str
    fields: dict[str, Any]
    expected_updated_at: str


class WorkflowThreadDraftResponse(BaseModel):
    agent_run_id: UUID
    thread_id: str
    stage: str
    status: str
    pending_action_id: UUID
    message: str


# ---------------------------------------------------------------------------
# POST /workflow-threads/{thread_id}/decisions -- one generic endpoint with a
# `decision_type` discriminator, replacing the three separate decision/
# qty-mismatch-decision/manual-cmir-entry endpoints (approved plan §6).
# ---------------------------------------------------------------------------


class CmirApprovalDecisionRequest(BaseModel):
    """`decision_type="CMIR_APPROVAL"` -- was `POST /threads/{id}/decision`."""

    decision_type: Literal["CMIR_APPROVAL"] = "CMIR_APPROVAL"
    actor: str
    decision: Literal["approve", "reject"]
    expected_updated_at: str
    reason: str = ""


class QtyMismatchDecisionRequest(BaseModel):
    """`decision_type="QTY_MISMATCH"` -- was `POST /threads/{id}/qty-mismatch-decision`."""

    decision_type: Literal["QTY_MISMATCH"] = "QTY_MISMATCH"
    actor: str
    decision: Literal["use_substitute", "proceed_anyway", "mark_stale"]
    substitute_material_code: str | None = None
    expected_updated_at: str


class ManualCmirEntryDecisionRequest(BaseModel):
    """`decision_type="MANUAL_CMIR_ENTRY"` -- was `POST /threads/{id}/manual-cmir-entry`."""

    decision_type: Literal["MANUAL_CMIR_ENTRY"] = "MANUAL_CMIR_ENTRY"
    actor: str
    sap_material_number: str
    description: str = ""
    expected_updated_at: str


WorkflowThreadDecisionRequest = Annotated[
    CmirApprovalDecisionRequest | QtyMismatchDecisionRequest | ManualCmirEntryDecisionRequest,
    Field(discriminator="decision_type"),
]
