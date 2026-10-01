"""API endpoints for CMIR email event ingestion."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_service
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import ValidationError
from app.schemas.cmir.email_events import (
    IngestEmailEventsRequest,
    IngestEmailEventsResponse,
    PendingEmailSummary,
)
from app.schemas.cmir.health import CMIRHealthSnapshotResponse, HealthTrendPoint, HousekeepingAuditLogEntry
from app.services.cmir.service import CmirService

router = APIRouter(tags=["cmir"])


@router.get("/cmir-records/health", response_model=Envelope[CMIRHealthSnapshotResponse])
def get_cmir_health(
    run_service: Annotated[CmirService, Depends(get_service)],
) -> Envelope[CMIRHealthSnapshotResponse]:
    result = run_service.get_health_snapshot()
    return success_envelope(CMIRHealthSnapshotResponse.model_validate(result))


@router.get("/cmir-records/health-trend", response_model=Envelope[list[HealthTrendPoint]])
def get_cmir_health_trend(
    run_service: Annotated[CmirService, Depends(get_service)],
    months: int = 6,
) -> Envelope[list[HealthTrendPoint]]:
    result = run_service.get_health_trend(months=months)
    return success_envelope([HealthTrendPoint.model_validate(r) for r in result])


@router.get(
    "/cmir-records/housekeeping-audit-log",
    response_model=Envelope[list[HousekeepingAuditLogEntry]],
)
def get_cmir_housekeeping_audit_log(
    run_service: Annotated[CmirService, Depends(get_service)],
    limit: int = 10,
) -> Envelope[list[HousekeepingAuditLogEntry]]:
    result = run_service.get_housekeeping_audit_log(limit=limit)
    return success_envelope([HousekeepingAuditLogEntry.model_validate(r) for r in result])


@router.post(
    "/cmir/email-events",
    response_model=Envelope[IngestEmailEventsResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
def start_email_ingest(
    body: IngestEmailEventsRequest,
    run_service: Annotated[CmirService, Depends(get_service)],
) -> Envelope[IngestEmailEventsResponse]:
    """Start email ingestion from Gmail.

    Rejects any `source` other than `"gmail"` as invalid, since no other
    source is currently configured, then kicks off ingestion and returns
    202 accepted.
    """
    if body.source != "gmail":
        raise ValidationError(
            code="VALIDATION_ERROR",
            message="Only gmail source is currently configured.",
            details={"source": body.source},
        )
    result = run_service.start_email_ingest(
        max_workers=body.max_workers,
        subject_contains=body.filters.subject_contains,
        unread_only=body.filters.unread_only,
    )
    return success_envelope(IngestEmailEventsResponse.model_validate(result), message="Email ingest started.")


@router.get("/cmir/email-events/pending", response_model=Envelope[list[PendingEmailSummary]])
def list_pending_emails(
    run_service: Annotated[CmirService, Depends(get_service)],
    limit: int = 50,
) -> Envelope[list[PendingEmailSummary]]:
    """The UI "email queue" panel -- every queued email not yet fully
    processed, so a reviewer can manually process one instead of waiting on
    the real Service Bus consumer."""
    result = run_service.list_pending_emails(limit=limit)
    return success_envelope([PendingEmailSummary.model_validate(r) for r in result])


@router.post("/cmir/email-events/{email_id}/process", response_model=Envelope[dict[str, Any]])
def process_pending_email(
    email_id: UUID,
    run_service: Annotated[CmirService, Depends(get_service)],
) -> Envelope[dict[str, Any]]:
    """UI-triggered "unqueue" action for one row from `list_pending_emails`
    -- reuses `process_queued_email`'s exact graph logic, just supplies
    batch_id/queue_message_id/content itself instead of requiring the
    caller to build a Service-Bus-shaped payload. Same polymorphic,
    untyped result as `POST /internal/process-email`
    (`app/api/v1/internal.py`, which owns that route)."""
    result = run_service.process_pending_email(email_id)
    return success_envelope(result)
