"""API schemas for fulfillment facts: confirmations, shipments, exceptions, actual penalties.

Every fact is keyed to a `purchase_order_line_id`, except shipments, which attach to a delivery.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class OrderConfirmationLineCreate(BaseModel):
    """One line of a `POST /order-confirmations` request."""

    purchase_order_line_id: UUID
    confirmed_quantity: float
    confirmed_delivery_date: date | None = None
    cut_reason_code: str | None = None


class OrderConfirmationLineResponse(BaseModel):
    """Response shape for one `order_confirmation_line` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_confirmation_id: UUID
    purchase_order_line_id: UUID
    confirmed_quantity: float
    confirmed_delivery_date: date | None = None
    cut_reason_code: str | None = None


class OrderConfirmationRequest(BaseModel):
    """Request body for `POST /order-confirmations`, recording a retailer's confirmation."""

    confirmation_number: str
    confirmation_date: datetime
    status: str | None = None
    # When omitted, applies to the PO's first line: the same single-primary-line convenience
    # `ProjectionService.build_snapshot` and the seed data use.
    lines: list[OrderConfirmationLineCreate] = []


class OrderConfirmationResponse(BaseModel):
    """Response shape for an `order_confirmation` row, including its lines."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    confirmation_number: str
    purchase_order_id: UUID
    confirmation_date: datetime
    status: str | None = None
    lines: list[OrderConfirmationLineResponse] = []


class ShipmentRequest(BaseModel):
    """Request body for recording a shipment fact against a delivery."""

    shipment_number: str
    delivery_number: str | None = None
    carrier_id: UUID | None = None
    expected_ship_date: date | None = None
    actual_ship_date: date | None = None
    expected_delivery_date: date | None = None
    actual_delivery_date: date | None = None
    appointment_status: Literal["SCHEDULED", "RESCHEDULED", "MISSED", "COMPLETED"] = "SCHEDULED"
    expected_transit_days: int = 2
    shipment_status: str = "SCHEDULED"
    recorded_at: datetime


class ShipmentResponse(BaseModel):
    """Response shape for a `shipment` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    shipment_number: str
    delivery_id: UUID
    carrier_id: UUID | None = None
    expected_ship_date: date | None = None
    actual_ship_date: date | None = None
    expected_delivery_date: date | None = None
    actual_delivery_date: date | None = None
    expected_transit_days: int | None = None
    appointment_status: str | None = None
    shipment_status: str
    recorded_at: datetime


class DemandExceptionRequest(BaseModel):
    """Request body for flagging a demand exception against a purchase order line."""

    exception_id: str
    purchase_order_line_id: UUID | None = None
    flagged_date: date


class DemandExceptionResponse(BaseModel):
    """Response shape for a `demand_exception` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    exception_id: str
    purchase_order_line_id: UUID
    flagged_date: date
    resolved: bool


class ActualPenaltyRequest(BaseModel):
    """Request body for recording an actual, post-delivery penalty against a purchase order."""

    purchase_order_id: UUID
    purchase_order_line_id: UUID | None = None
    actual_penalty_number: str
    violation_type: str
    actual_penalty_amount: float
    invoice_or_deduction_date: date
    dispute_status: str = "NONE"


class ActualPenaltyResponse(BaseModel):
    """Response shape for an `actual_penalty` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actual_penalty_number: str
    purchase_order_id: UUID
    purchase_order_line_id: UUID | None = None
    violation_type: str
    actual_penalty_amount: float
    invoice_or_deduction_date: date
    dispute_status: str
