"""API endpoints for the PO Validation ingest pipeline and `purchase_order_line`
listing.

Was `app/api/v1/po_validation.py`'s `create_router()` factory (stale imports
-- schemas/exceptions that no longer exist post-restructure). Rewritten per
the approved plan §6: module-level `router = APIRouter(...)` (Phase 7a's
convention), `Envelope[T]` on every route, the collapsed `AppError`
hierarchy.

| Old | New |
|---|---|
| `POST /ingest/po-lines` | `POST /api/v1/po-validation/purchase-order-lines` |
| `GET /po-lines` | `GET /api/v1/purchase-order-lines?status=...` (flat, cross-PO) + `GET /api/v1/purchase-orders/{purchase_order_id}/lines` (nested, single-PO -- built as well since `common.purchase_order_line` listing for one known PO already has full repository support) |
| `GET /po-lines/{id}/errors` | `GET /api/v1/processing-errors?purchase_order_line_id={id}` (`app/api/v1/processing_errors.py`) |
| `POST /threads/{id}/qty-mismatch-decision`, `POST /threads/{id}/manual-cmir-entry` | `POST /api/v1/workflow-threads/{thread_id}/decisions` (`app/api/v1/workflow_threads.py`) |
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query

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
    status_code=202,
)
def create_purchase_order_lines(
    body: IngestPurchaseOrderLinesRequest,
    po_service: PoValidationService = Depends(get_po_service),
) -> Envelope[IngestPurchaseOrderLinesResponse]:
    """Domain-scoped ingest action creating `common.purchase_order_line` rows
    (approved plan §6 Phase-0 decision): kept under `/po-validation/` since
    it's the validation pipeline's entry point (runs CMIR matching/
    material-master checks as a side effect of ingest), not a generic
    `common.purchase_order_line` CRUD create."""
    result = po_service.ingest_po_lines([line.model_dump() for line in body.lines])
    return success_envelope(
        IngestPurchaseOrderLinesResponse.model_validate(result), message="Purchase order lines ingested."
    )


@router.get("/purchase-order-lines", response_model=Envelope[PurchaseOrderLinesListResponse])
def list_purchase_order_lines(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    po_service: PoValidationService = Depends(get_po_service),
) -> Envelope[PurchaseOrderLinesListResponse]:
    """Flat, cross-PO listing (approved plan §5/§6's naming rule) -- see
    `PurchaseOrderLinesListResponse`'s docstring for the current
    `VIEW_NOT_SUPPORTED` capability gap this surfaces rather than fakes."""
    result = po_service.list_ready_lines(status=status, limit=limit, cursor=cursor)
    return success_envelope(PurchaseOrderLinesListResponse.model_validate(result))


@router.get(
    "/purchase-orders/{purchase_order_id}/lines",
    response_model=Envelope[list[PurchaseOrderLineResponse]],
)
def list_purchase_order_lines_for_order(
    purchase_order_id: UUID,
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
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
    limit: int = Query(default=5, ge=1, le=50),
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
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
