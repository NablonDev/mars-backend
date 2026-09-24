"""API endpoints for actual penalties incurred by purchase orders."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_actual_penalty_repository, get_purchase_order_repository
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import NotFoundError
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.projection import ActualPenaltyRepository
from app.schemas.common.fulfillment import ActualPenaltyRequest, ActualPenaltyResponse

router = APIRouter(prefix="/penalties", tags=["actual-penalties"])


@router.post(
    "/actual-penalties",
    response_model=Envelope[ActualPenaltyResponse],
    status_code=status.HTTP_201_CREATED,
)
def add_actual_penalty(
    body: ActualPenaltyRequest,
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    actual_penalties: Annotated[ActualPenaltyRepository, Depends(get_actual_penalty_repository)],
) -> Envelope[ActualPenaltyResponse]:
    """Record an actual penalty incurred for a purchase order.

    404s if the purchase order doesn't exist before recording anything.
    """
    purchase_orders.require_purchase_order(body.purchase_order_id)
    created = actual_penalties.add_actual_penalty(**body.model_dump())
    return success_envelope(ActualPenaltyResponse.model_validate(created), message="Actual penalty recorded.")


@router.get(
    "/actual-penalties",
    response_model=Envelope[list[ActualPenaltyResponse]],
)
def list_actual_penalties(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    actual_penalties: Annotated[ActualPenaltyRepository, Depends(get_actual_penalty_repository)],
    purchase_order_id: Annotated[UUID | None, Query()] = None,
) -> Envelope[list[ActualPenaltyResponse]]:
    """List incurred penalties, optionally narrowed to one purchase order.

    Naming a purchase order that does not exist is a 404.
    """
    if purchase_order_id is not None:
        purchase_orders.require_purchase_order(purchase_order_id)
    rows = actual_penalties.list_actual_penalties(purchase_order_id=purchase_order_id)
    return success_envelope([ActualPenaltyResponse.model_validate(r) for r in rows])


# NOTE: this prefix has no literal sibling route (no `/summary` action), so
# `{actual_penalty_id}` cannot shadow one the way it can in
# `app.api.v1.penalties.projections`/`mitigations`. Registered last anyway,
# for consistency with those modules.
@router.get(
    "/actual-penalties/{actual_penalty_id}",
    response_model=Envelope[ActualPenaltyResponse],
)
def get_actual_penalty(
    actual_penalty_id: UUID,
    actual_penalties: Annotated[ActualPenaltyRepository, Depends(get_actual_penalty_repository)],
) -> Envelope[ActualPenaltyResponse]:
    """Standalone fetch by the row's own surrogate id."""
    row = actual_penalties.get(actual_penalty_id)
    if row is None:
        raise NotFoundError(
            code="ACTUAL_PENALTY_NOT_FOUND",
            message=f"No actual penalty found with actual_penalty_id={actual_penalty_id}",
        )
    return success_envelope(ActualPenaltyResponse.model_validate(row))
