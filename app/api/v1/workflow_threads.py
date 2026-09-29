"""API endpoints for workflow thread lifecycle management."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_po_service, get_service, parse_include
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import NotFoundError
from app.schemas.cmir.threads import (
    CmirApprovalDecisionRequest,
    CmirThreadSnapshotResponse,
    ManualCmirEntryDecisionRequest,
    QtyMismatchDecisionRequest,
    WorkflowThreadDecisionRequest,
    WorkflowThreadDetailResponse,
    WorkflowThreadDraftResponse,
    WorkflowThreadFieldsRequest,
    WorkflowThreadListResponse,
    WorkflowThreadResponse,
)
from app.schemas.po_validation.threads import PoValidationThreadSnapshotResponse
from app.services.cmir.service import CmirService
from app.services.po_validation.service import PoValidationService

router = APIRouter(tags=["workflow-threads"])

_INCLUDE_SNAPSHOT = parse_include(frozenset({"snapshot"}))


@router.get("/workflow-threads", response_model=Envelope[WorkflowThreadListResponse])
def list_workflow_threads(
    run_service: Annotated[CmirService, Depends(get_service)],
    domain: Annotated[Literal["cmir", "po_validation"] | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    stage: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Envelope[WorkflowThreadListResponse]:
    """List workflow threads, optionally filtered by domain, status, or stage."""
    result = run_service.list_runs(view="threads", status=status, stage=stage, limit=limit, cursor=cursor)
    items = result["items"]
    if domain == "cmir":
        items = [item for item in items if item.get("email_event_id") is not None]
    elif domain == "po_validation":
        items = [item for item in items if item.get("purchase_order_line_id") is not None]
    return success_envelope(
        WorkflowThreadListResponse(
            items=[WorkflowThreadResponse.model_validate(item) for item in items],
            next_cursor=result["next_cursor"],
        )
    )


@router.get(
    "/workflow-threads/{thread_id}",
    response_model=Envelope[WorkflowThreadDetailResponse],
)
def get_workflow_thread(
    thread_id: UUID,
    run_service: Annotated[CmirService, Depends(get_service)],
    po_run_service: Annotated[PoValidationService, Depends(get_po_service)],
    include: Annotated[set[str], Depends(_INCLUDE_SNAPSHOT)],
) -> Envelope[WorkflowThreadDetailResponse]:
    """Get a workflow thread's stage and optionally its snapshot.

    With `include=snapshot`, tries the PO-validation snapshot first and
    falls back to the CMIR snapshot when the thread isn't a PO-validation
    one, since `workflow_thread` is a shared resource not owned by either
    domain.
    """
    stage = run_service.get_stage(thread_id)

    snapshot: dict | None = None
    if "snapshot" in include:
        try:
            po_snapshot = po_run_service.get_snapshot(thread_id)
            snapshot = PoValidationThreadSnapshotResponse.model_validate(po_snapshot).model_dump(mode="json")
        except NotFoundError as exc:
            if exc.code != "THREAD_NOT_FOUND":
                raise
            cmir_snapshot = run_service.get_snapshot(thread_id)
            snapshot = CmirThreadSnapshotResponse.model_validate(cmir_snapshot).model_dump(mode="json")

    return success_envelope(WorkflowThreadDetailResponse(**stage, snapshot=snapshot))


@router.post(
    "/workflow-threads/{thread_id}/missing-fields",
    response_model=Envelope[WorkflowThreadResponse],
)
def submit_workflow_thread_missing_fields(
    thread_id: UUID,
    body: WorkflowThreadFieldsRequest,
    run_service: Annotated[CmirService, Depends(get_service)],
) -> Envelope[WorkflowThreadResponse]:
    """Submit missing field values to advance a workflow thread requiring completion."""
    result = run_service.submit_missing_fields(
        thread_id,
        actor=body.actor,
        fields=body.fields,
        expected_updated_at=body.expected_updated_at,
    )
    return success_envelope(WorkflowThreadResponse.model_validate(result))


@router.patch(
    "/workflow-threads/{thread_id}/draft",
    response_model=Envelope[WorkflowThreadDraftResponse],
)
def update_workflow_thread_draft(
    thread_id: UUID,
    body: WorkflowThreadFieldsRequest,
    run_service: Annotated[CmirService, Depends(get_service)],
) -> Envelope[WorkflowThreadDraftResponse]:
    """Save field values to the in-flight review draft without submitting it.

    A `PATCH` on the draft sub-resource distinct from
    `submit_workflow_thread_missing_fields`, which advances the thread.
    """
    result = run_service.update_draft(
        thread_id,
        actor=body.actor,
        fields=body.fields,
        expected_updated_at=body.expected_updated_at,
    )
    return success_envelope(WorkflowThreadDraftResponse.model_validate(result), message="Draft saved.")


@router.post(
    "/workflow-threads/{thread_id}/decisions",
    response_model=Envelope[WorkflowThreadResponse],
)
def submit_workflow_thread_decision(
    thread_id: UUID,
    body: WorkflowThreadDecisionRequest,
    run_service: Annotated[CmirService, Depends(get_service)],
    po_run_service: Annotated[PoValidationService, Depends(get_po_service)],
) -> Envelope[WorkflowThreadResponse]:
    """Record a decision on a workflow thread, discriminated by `decision_type`.

    Covers CMIR approval decisions and both `po_validation` decision types.
    """
    if isinstance(body, CmirApprovalDecisionRequest):
        result = run_service.submit_decision(
            thread_id,
            actor=body.actor,
            decision=body.decision,
            expected_updated_at=body.expected_updated_at,
            reason=body.reason,
        )
    elif isinstance(body, QtyMismatchDecisionRequest):
        result = po_run_service.submit_qty_mismatch_decision(
            thread_id,
            actor=body.actor,
            decision=body.decision,
            substitute_material_code=body.substitute_material_code,
            expected_updated_at=body.expected_updated_at,
        )
    else:
        assert isinstance(body, ManualCmirEntryDecisionRequest)
        result = po_run_service.submit_manual_cmir_entry(
            thread_id,
            actor=body.actor,
            sap_material_number=body.sap_material_number,
            description=body.description,
            expected_updated_at=body.expected_updated_at,
        )
    return success_envelope(WorkflowThreadResponse.model_validate(result))
