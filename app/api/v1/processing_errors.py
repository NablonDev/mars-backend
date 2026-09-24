"""API endpoint for listing processing errors."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_po_service
from app.core.envelope import Envelope, success_envelope
from app.schemas.po_validation.processing_errors import ProcessingErrorsListResponse
from app.services.po_validation.service import PoValidationService

router = APIRouter(tags=["processing-errors"])


@router.get("/processing-errors", response_model=Envelope[ProcessingErrorsListResponse])
def list_processing_errors(
    purchase_order_line_id: Annotated[UUID, Query()],
    po_service: Annotated[PoValidationService, Depends(get_po_service)],
) -> Envelope[ProcessingErrorsListResponse]:
    """List all processing errors for a purchase order line."""
    result = po_service.get_errors(purchase_order_line_id)
    return success_envelope(ProcessingErrorsListResponse.model_validate(result))
