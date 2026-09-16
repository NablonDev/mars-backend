"""API endpoints for PO validation and purchase order line listing."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_po_service, get_purchase_order_repository
from app.core.envelope import Envelope, success_envelope
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.schemas.po_validation.purchase_order_lines import (
    IngestPurchaseOrderLinesRequest,
    IngestPurchaseOrderLinesResponse,
    PurchaseOrderLinesListResponse,
)
from app.services.po_validation.service import PoValidationService

router = APIRouter(tags=["po-validation"])


@router.post(
    "/po-validation/purchase-order-lines",
    response_model=Envelope[IngestPurchaseOrderLinesResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
def ingest_purchase_order_lines(
    body: IngestPurchaseOrderLinesRequest,
    po_service: PoValidationService = Depends(get_po_service),
) -> Envelope[IngestPurchaseOrderLinesResponse]:
    """Create purchase order lines and run the validation pipeline over them.

    CMIR matching and material-master checks run as a side effect of ingest.
    """
    result = po_service.ingest_po_lines([line.model_dump() for line in body.lines])
    return success_envelope(
        IngestPurchaseOrderLinesResponse.model_validate(result), message="Purchase order lines ingested."
    )


@router.get("/purchase-order-lines", response_model=Envelope[PurchaseOrderLinesListResponse])
def list_purchase_order_lines(
    purchase_order_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    po_service: PoValidationService = Depends(get_po_service),
) -> Envelope[PurchaseOrderLinesListResponse]:
    """List purchase order lines, optionally narrowed by purchase order or status."""
    if purchase_order_id is not None:
        purchase_orders.require_purchase_order(purchase_order_id)
    result = po_service.list_ready_lines(
        purchase_order_id=purchase_order_id, status=status, limit=limit, cursor=cursor
    )
    return success_envelope(PurchaseOrderLinesListResponse.model_validate(result))
