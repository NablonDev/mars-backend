"""Internal Service Bus consumer callback for CMIR email processing.

`POST /internal/process-email` is the queue consumer's own call, not a
PRD-facing route, and so is kept separate from the public
`cmir/email-events` resource.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_service
from app.core.envelope import Envelope, success_envelope
from app.schemas.cmir.email_events import ProcessQueuedEmailRequest
from app.services.cmir.service import CmirService

router = APIRouter(tags=["internal"])


@router.post("/internal/process-email", response_model=Envelope[dict[str, Any]])
def process_queued_email(
    body: ProcessQueuedEmailRequest,
    run_service: CmirService = Depends(get_service),
) -> Envelope[dict[str, Any]]:
    """Process one queued CMIR email on behalf of the Service Bus consumer.

    The result shape is polymorphic, so it stays an untyped dict rather than
    forcing one response model onto genuinely different shapes.
    """
    result = run_service.process_queued_email(
        batch_id=body.batch_id,
        email=body.email,
        queue_message_id=body.queue_message_id,
        email_id=body.email_id,
    )
    return success_envelope(result)
