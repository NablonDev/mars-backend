"""API endpoints for purchase order header and line CRUD."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_purchase_order_repository
from app.core.envelope import Envelope, success_envelope
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.schemas.common.purchase_orders import (
    PurchaseOrderLineResponse,
    PurchaseOrderRequest,
    PurchaseOrderResponse,
)

router = APIRouter(tags=["purchase-orders"])


def _to_response(purchase_orders: PurchaseOrderRepository, po: dict) -> PurchaseOrderResponse:
    """Convert a purchase order row to a response model with its lines included."""
    lines = [PurchaseOrderLineResponse.model_validate(line) for line in purchase_orders.list_lines(po["id"])]
    return PurchaseOrderResponse.model_validate({**po, "lines": lines})


@router.post(
    "/purchase-orders",
    response_model=Envelope[PurchaseOrderResponse],
    status_code=status.HTTP_201_CREATED,
)
def create_purchase_order(
    body: PurchaseOrderRequest,
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
) -> Envelope[PurchaseOrderResponse]:
    """Create a purchase order header with line items.

    Persists the header first, then adds each of `body.lines` against it,
    so the response always reflects the fully created order with lines.
    """
    fields = body.model_dump(exclude={"lines"})
    purchase_order_number = fields.pop("purchase_order_number")
    po = purchase_orders.create_purchase_order(purchase_order_number, **fields)
    for line in body.lines:
        purchase_orders.add_line(purchase_order_id=po["id"], **line.model_dump())
    return success_envelope(_to_response(purchase_orders, po), message="Purchase order created.")


@router.get("/purchase-orders", response_model=Envelope[list[PurchaseOrderResponse]])
def list_purchase_orders(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    order_status: Annotated[str | None, Query()] = None,
) -> Envelope[list[PurchaseOrderResponse]]:
    """List all purchase orders, optionally filtered by order status."""
    rows = [_to_response(purchase_orders, po) for po in purchase_orders.list_purchase_orders(order_status)]
    return success_envelope(rows)
