"""API schemas for master data, purchase orders, fulfillment facts, and delivery changes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.schemas.common.delivery_change_requests import (
    DeliveryChangeRequestCreate,
    DeliveryChangeRequestResponse,
    DeliveryChangeResponseRequest,
)
from app.schemas.common.fulfillment import (
    ActualPenaltyRequest,
    ActualPenaltyResponse,
    DemandExceptionRequest,
    DemandExceptionResponse,
    OrderConfirmationLineCreate,
    OrderConfirmationLineResponse,
    OrderConfirmationRequest,
    OrderConfirmationResponse,
    ShipmentRequest,
    ShipmentResponse,
)
from app.schemas.common.master_data import (
    CarrierRequest,
    CarrierResponse,
    MaterialMasterRequest,
    MaterialMasterResponse,
    MaterialRequest,
    MaterialResponse,
    PlantRequest,
    PlantResponse,
    RetailerLocationRequest,
    RetailerLocationResponse,
    RetailerRequest,
    RetailerResponse,
    SkuRequest,
    SkuResponse,
)
from app.schemas.common.purchase_orders import (
    PurchaseOrderLineCreate,
    PurchaseOrderLineResponse,
    PurchaseOrderRequest,
    PurchaseOrderResponse,
)


class HealthResponse(BaseModel):
    """Response shape for the service health-check endpoint."""

    status: Literal["ok", "degraded"]
    database: Literal["ok", "unreachable"]


__all__ = [
    "ActualPenaltyRequest",
    "ActualPenaltyResponse",
    "CarrierRequest",
    "CarrierResponse",
    "DeliveryChangeRequestCreate",
    "DeliveryChangeRequestResponse",
    "DeliveryChangeResponseRequest",
    "DemandExceptionRequest",
    "DemandExceptionResponse",
    "HealthResponse",
    "MaterialMasterRequest",
    "MaterialMasterResponse",
    "MaterialRequest",
    "MaterialResponse",
    "OrderConfirmationLineCreate",
    "OrderConfirmationLineResponse",
    "OrderConfirmationRequest",
    "OrderConfirmationResponse",
    "PlantRequest",
    "PlantResponse",
    "PurchaseOrderLineCreate",
    "PurchaseOrderLineResponse",
    "PurchaseOrderRequest",
    "PurchaseOrderResponse",
    "RetailerLocationRequest",
    "RetailerLocationResponse",
    "RetailerRequest",
    "RetailerResponse",
    "ShipmentRequest",
    "ShipmentResponse",
    "SkuRequest",
    "SkuResponse",
]
