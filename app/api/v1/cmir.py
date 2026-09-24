"""API endpoints for CMIR email event ingestion."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_service
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import ValidationError
from app.schemas.cmir.email_events import IngestEmailEventsRequest, IngestEmailEventsResponse
from app.services.cmir.service import CmirService

router = APIRouter(tags=["cmir"])


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
