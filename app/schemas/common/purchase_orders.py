"""API schemas for `purchase_order` and `purchase_order_line`, kept as separate shapes.

A confirmation or delivery can partially cover a multi-line PO, so the two never collapse.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PurchaseOrderLineCreate(BaseModel):
    """One line of a `POST /purchase-orders` request."""

    line_number: str
    retailer_po_line_number: str | None = None
    sku_id: UUID | None = None
    material_id: UUID | None = None
    retailer_material_code: str | None = None
    plant_id: UUID | None = None
    storage_location_id: UUID | None = None
    ship_to_location_id: UUID | None = None
    ordered_quantity: float
    unit_price: float
    uom: str | None = None
    requested_delivery_date: date | None = None
    required_ship_date: date | None = None


class PurchaseOrderLineResponse(BaseModel):
    """Response shape for a `purchase_order_line` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    purchase_order_id: UUID
    line_number: str
    retailer_po_line_number: str | None = None
    sku_id: UUID | None = None
    material_id: UUID | None = None
    retailer_material_code: str | None = None
    plant_id: UUID | None = None
    storage_location_id: UUID | None = None
    ship_to_location_id: UUID | None = None
    ordered_quantity: float
    unit_price: float
    uom: str | None = None
    requested_delivery_date: date | None = None
    required_ship_date: date | None = None
    line_status: str


class PurchaseOrderRequest(BaseModel):
    """Request body for `POST /purchase-orders`, creating a PO header with its nested lines."""

    purchase_order_number: str
    retailer_id: UUID
    retailer_po_number: str | None = None
    order_date: date
    requested_delivery_date: date | None = None
    required_ship_date: date | None = None
    order_status: str = "OPEN"
    source_system: str | None = None
    source_document_type: str | None = None
    source_document_number: str | None = None
    lines: list[PurchaseOrderLineCreate] = []


class PurchaseOrderResponse(BaseModel):
    """Response shape for a `purchase_order` row, including its lines."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    purchase_order_number: str
    retailer_id: UUID
    retailer_po_number: str | None = None
    order_date: date
    requested_delivery_date: date | None = None
    required_ship_date: date | None = None
    order_status: str
    source_system: str | None = None
    source_document_type: str | None = None
    source_document_number: str | None = None
    current_delivery_date: date | None = None
    current_required_ship_date: date | None = None
    negotiation_status: str
    lines: list[PurchaseOrderLineResponse] = []
