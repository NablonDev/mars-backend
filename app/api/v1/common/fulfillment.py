"""API endpoints for purchase order fulfillment facts."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_fulfillment_repository, get_purchase_order_repository
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import ValidationError
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.schemas.common.fulfillment import (
    DemandExceptionRequest,
    DemandExceptionResponse,
    OrderConfirmationLineResponse,
    OrderConfirmationRequest,
    OrderConfirmationResponse,
    ShipmentRequest,
    ShipmentResponse,
)

router = APIRouter(tags=["purchase-orders"])


def _first_line_id(purchase_orders: PurchaseOrderRepository, purchase_order_id: UUID) -> UUID:
    """Resolve the first line ID for a purchase order, or raise if no lines exist."""
    lines = purchase_orders.list_lines(purchase_order_id)
    if not lines:
        raise ValidationError(
            code="PO_HAS_NO_LINES",
            message=(
                f"Purchase order {purchase_order_id} has no lines: add one first, or pass an "
                "explicit purchase_order_line_id."
            ),
        )
    return lines[0]["id"]


# ---------------------------------------------------------------------------
# Order confirmations
# ---------------------------------------------------------------------------


@router.post(
    "/purchase-orders/{purchase_order_id}/confirmations",
    response_model=Envelope[OrderConfirmationResponse],
    status_code=status.HTTP_201_CREATED,
)
def add_confirmation(
    purchase_order_id: UUID,
    body: OrderConfirmationRequest,
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    fulfillment: Annotated[FulfillmentRepository, Depends(get_fulfillment_repository)],
) -> Envelope[OrderConfirmationResponse]:
    """Record an order confirmation with line-level confirmed quantities and delivery dates."""
    purchase_orders.require_purchase_order(purchase_order_id)

    confirmation = fulfillment.add_order_confirmation(
        confirmation_number=body.confirmation_number,
        purchase_order_id=purchase_order_id,
        confirmation_date=body.confirmation_date,
        status=body.status,
    )

    # No default line here (unlike demand-exceptions/shipments below):
    # a confirmation always carries a real confirmed_quantity per line, and
    # guessing "the PO's ordered_quantity" would silently invent a fact
    # instead of recording one. Callers must pass `lines` explicitly.
    lines = [
        fulfillment.add_order_confirmation_line(
            order_confirmation_id=confirmation["id"],
            purchase_order_line_id=line.purchase_order_line_id,
            confirmed_quantity=line.confirmed_quantity,
            confirmed_delivery_date=line.confirmed_delivery_date,
            cut_reason_code=line.cut_reason_code,
        )
        for line in body.lines
    ]
    return success_envelope(
        OrderConfirmationResponse.model_validate({**confirmation, "lines": lines}),
        message="Confirmation recorded.",
    )


@router.get(
    "/purchase-orders/{purchase_order_id}/confirmations",
    response_model=Envelope[list[OrderConfirmationLineResponse]],
)
def list_confirmations(
    purchase_order_id: UUID,
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    fulfillment: Annotated[FulfillmentRepository, Depends(get_fulfillment_repository)],
) -> Envelope[list[OrderConfirmationLineResponse]]:
    """Return confirmation history across every line of the purchase order.

    `FulfillmentRepository` offers per-line lookups only, so this aggregates
    across the PO's own lines rather than returning nested confirmation
    headers.
    """
    purchase_orders.require_purchase_order(purchase_order_id)
    rows = [
        row
        for line in purchase_orders.list_lines(purchase_order_id)
        for row in fulfillment.list_confirmation_lines_for_line(line["id"])
    ]
    return success_envelope([OrderConfirmationLineResponse.model_validate(r) for r in rows])


# ---------------------------------------------------------------------------
# Shipments (via a per-PO delivery header)
# ---------------------------------------------------------------------------


@router.post(
    "/purchase-orders/{purchase_order_id}/shipments",
    response_model=Envelope[ShipmentResponse],
    status_code=status.HTTP_201_CREATED,
)
def record_shipment(
    purchase_order_id: UUID,
    body: ShipmentRequest,
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    fulfillment: Annotated[FulfillmentRepository, Depends(get_fulfillment_repository)],
) -> Envelope[ShipmentResponse]:
    """Record a shipment event for a purchase order.

    Creates the delivery header the shipment attaches to on the fly,
    defaulting `delivery_number` to `DELIV-{purchase_order_number}` when
    the caller doesn't supply one.
    """
    purchase_order = purchase_orders.require_purchase_order(purchase_order_id)

    delivery_number = body.delivery_number or f"DELIV-{purchase_order['purchase_order_number']}"
    delivery = fulfillment.add_delivery(delivery_number=delivery_number, purchase_order_id=purchase_order_id)

    shipment = fulfillment.add_shipment(
        shipment_number=body.shipment_number,
        delivery_id=delivery["id"],
        recorded_at=body.recorded_at,
        carrier_id=body.carrier_id,
        expected_ship_date=body.expected_ship_date,
        actual_ship_date=body.actual_ship_date,
        expected_delivery_date=body.expected_delivery_date,
        actual_delivery_date=body.actual_delivery_date,
        expected_transit_days=body.expected_transit_days,
        appointment_status=body.appointment_status,
        shipment_status=body.shipment_status,
    )
    return success_envelope(ShipmentResponse.model_validate(shipment), message="Shipment recorded.")


@router.get(
    "/purchase-orders/{purchase_order_id}/shipments",
    response_model=Envelope[list[ShipmentResponse]],
)
def list_shipments(
    purchase_order_id: UUID,
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    fulfillment: Annotated[FulfillmentRepository, Depends(get_fulfillment_repository)],
) -> Envelope[list[ShipmentResponse]]:
    """List all shipments recorded for a purchase order.

    404s if the purchase order itself doesn't exist.
    """
    purchase_orders.require_purchase_order(purchase_order_id)
    rows = fulfillment.list_shipments_for_purchase_order(purchase_order_id)
    return success_envelope([ShipmentResponse.model_validate(r) for r in rows])


# ---------------------------------------------------------------------------
# Demand exceptions
# ---------------------------------------------------------------------------


@router.post(
    "/purchase-orders/{purchase_order_id}/demand-exceptions",
    response_model=Envelope[DemandExceptionResponse],
    status_code=status.HTTP_201_CREATED,
)
def add_demand_exception(
    purchase_order_id: UUID,
    body: DemandExceptionRequest,
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    fulfillment: Annotated[FulfillmentRepository, Depends(get_fulfillment_repository)],
) -> Envelope[DemandExceptionResponse]:
    """Record a demand exception (e.g., shortage or cancellation) for a purchase order line."""
    purchase_orders.require_purchase_order(purchase_order_id)
    line_id = body.purchase_order_line_id or _first_line_id(purchase_orders, purchase_order_id)

    created = fulfillment.add_demand_exception(
        exception_id=body.exception_id,
        purchase_order_line_id=line_id,
        flagged_date=body.flagged_date,
    )
    return success_envelope(
        DemandExceptionResponse.model_validate(created), message="Demand exception recorded."
    )


@router.get(
    "/purchase-orders/{purchase_order_id}/demand-exceptions",
    response_model=Envelope[list[DemandExceptionResponse]],
)
def list_demand_exceptions(
    purchase_order_id: UUID,
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    fulfillment: Annotated[FulfillmentRepository, Depends(get_fulfillment_repository)],
) -> Envelope[list[DemandExceptionResponse]]:
    """List all demand exceptions recorded for a purchase order's lines."""
    purchase_orders.require_purchase_order(purchase_order_id)
    rows = [
        row
        for line in purchase_orders.list_lines(purchase_order_id)
        for row in fulfillment.list_demand_exceptions_for_line(line["id"])
    ]
    return success_envelope([DemandExceptionResponse.model_validate(r) for r in rows])
