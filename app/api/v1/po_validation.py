"""API endpoints for PO validation and purchase order line listing."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_po_service, get_purchase_order_repository
from app.core.envelope import Envelope, success_envelope
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.schemas.common.purchase_orders import PurchaseOrderLineResponse
from app.schemas.po_validation.purchase_order_lines import (
    IngestPurchaseOrderLinesRequest,
    IngestPurchaseOrderLinesResponse,
    PoAuditTrailLineResponse,
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
    po_service: Annotated[PoValidationService, Depends(get_po_service)],
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
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    po_service: Annotated[PoValidationService, Depends(get_po_service)],
    purchase_order_id: Annotated[UUID | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Envelope[PurchaseOrderLinesListResponse]:
    """List purchase order lines, optionally narrowed by purchase order or status."""
    if purchase_order_id is not None:
        purchase_orders.require_purchase_order(purchase_order_id)
    result = po_service.list_ready_lines(
        purchase_order_id=purchase_order_id, status=status, limit=limit, cursor=cursor
    )
    return success_envelope(PurchaseOrderLinesListResponse.model_validate(result))


@router.get(
    "/purchase-orders/{purchase_order_id}/lines",
    response_model=Envelope[list[PurchaseOrderLineResponse]],
)
def list_purchase_order_lines_for_order(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    purchase_order_id: UUID,
) -> Envelope[list[PurchaseOrderLineResponse]]:
    """Nested, single-PO listing -- `PurchaseOrderRepository.list_lines`
    already supports this directly (unlike the flat cross-PO listing
    above), so this route reads the repository directly rather than
    round-tripping through `PoValidationService`, mirroring
    `app/api/v1/common/purchase_orders.py`'s own convention for read-only
    nested listings."""
    purchase_orders.require_purchase_order(purchase_order_id)
    rows = purchase_orders.list_lines(purchase_order_id)
    return success_envelope([PurchaseOrderLineResponse.model_validate(r) for r in rows])


@router.get("/po-audit-trail", response_model=Envelope[list[PoAuditTrailLineResponse]])
def get_po_audit_trail(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    limit: Annotated[int, Query(ge=1, le=50)] = 5,
) -> Envelope[list[PoAuditTrailLineResponse]]:
    """CMIR Intelligence Module's "PO Audit Trail" panel -- the most
    recently created purchase_order_line rows across every PO. Direct
    repository read (no PoValidationService round-trip), mirroring
    `list_purchase_order_lines_for_order` above."""
    rows = purchase_orders.list_recent_lines(limit=limit)
    items = [
        PoAuditTrailLineResponse(
            po_number=r["purchase_order_number"],
            retailer_material_code=r["retailer_material_code"],
            quantity=r["ordered_quantity"],
            status=r["line_status"],
            delivery_date=r["delivery_date"].isoformat() if r["delivery_date"] else None,
        )
        for r in rows
    ]
    return success_envelope(items)
