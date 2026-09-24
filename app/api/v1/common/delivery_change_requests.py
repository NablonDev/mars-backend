"""API endpoints for PO delivery-date change requests."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_delivery_change_request_service, get_purchase_order_repository
from app.core.envelope import Envelope, success_envelope
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.schemas.common.delivery_change_requests import (
    DeliveryChangeRequestCreate,
    DeliveryChangeRequestResponse,
    DeliveryChangeResponseRequest,
)
from app.services.penalties.delivery_change import PoDeliveryChangeRequestService

router = APIRouter(tags=["po-delivery-change-requests"])


@router.post(
    "/delivery-change-requests",
    response_model=Envelope[DeliveryChangeRequestResponse],
    status_code=status.HTTP_201_CREATED,
)
def create_delivery_change_request(
    body: DeliveryChangeRequestCreate,
    service: Annotated[PoDeliveryChangeRequestService, Depends(get_delivery_change_request_service)],
) -> Envelope[DeliveryChangeRequestResponse]:
    """Submit a request to change a purchase order's delivery date."""
    created = service.create_request(
        body.purchase_order_id,
        body.reason_code,
        body.proposed_delivery_date,
        notes=body.notes,
    )
    return success_envelope(
        DeliveryChangeRequestResponse.model_validate(created), message="Delivery-change request created."
    )


@router.get(
    "/delivery-change-requests",
    response_model=Envelope[list[DeliveryChangeRequestResponse]],
)
def list_delivery_change_requests(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    service: Annotated[PoDeliveryChangeRequestService, Depends(get_delivery_change_request_service)],
    purchase_order_id: Annotated[UUID | None, Query()] = None,
) -> Envelope[list[DeliveryChangeRequestResponse]]:
    """List delivery-change requests, optionally narrowed to one purchase order.

    Naming a purchase order that does not exist is a 404.
    """
    if purchase_order_id is not None:
        purchase_orders.require_purchase_order(purchase_order_id)
    rows = [DeliveryChangeRequestResponse.model_validate(r) for r in service.list_history(purchase_order_id)]
    return success_envelope(rows)


# NOTE: no route under this prefix can be shadowed by
# `{delivery_change_request_id}` matching greedily; the only other one,
# `.../{delivery_change_request_id}/response`, always has one more path
# segment. Get-by-id is kept before that sub-action for readability only,
# mirroring the ordering `app.api.v1.penalties.projections`/`mitigations`
# genuinely need for their `/summary` sibling.
@router.get(
    "/delivery-change-requests/{delivery_change_request_id}",
    response_model=Envelope[DeliveryChangeRequestResponse],
)
def get_delivery_change_request(
    delivery_change_request_id: UUID,
    service: Annotated[PoDeliveryChangeRequestService, Depends(get_delivery_change_request_service)],
) -> Envelope[DeliveryChangeRequestResponse]:
    """Standalone fetch by the surrogate `id`."""
    row = service.get_by_id(delivery_change_request_id)
    return success_envelope(DeliveryChangeRequestResponse.model_validate(row))


@router.post(
    "/delivery-change-requests/{delivery_change_request_id}/response",
    response_model=Envelope[DeliveryChangeRequestResponse],
)
def record_delivery_change_response(
    delivery_change_request_id: UUID,
    body: DeliveryChangeResponseRequest,
    service: Annotated[PoDeliveryChangeRequestService, Depends(get_delivery_change_request_service)],
) -> Envelope[DeliveryChangeRequestResponse]:
    """Record a response (approval or counter-offer) to a delivery change request."""
    updated = service.record_response(
        delivery_change_request_id,
        body.decision,
        countered_delivery_date=body.countered_delivery_date,
    )
    return success_envelope(
        DeliveryChangeRequestResponse.model_validate(updated), message="Delivery-change response recorded."
    )
