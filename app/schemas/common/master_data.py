"""API schemas for master data: retailers, locations, SKUs, materials, plants, and carriers.

Request bodies carry the natural business code; response bodies add the surrogate `id`.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field


class RetailerRequest(BaseModel):
    """Request body for creating/updating a retailer master-data record."""

    retailer_code: str
    retailer_name: str
    priority_tier: str | None = None
    stacking_mode: str = Field(default="SUM", pattern="^(SUM|MAX)$")
    source_system: str | None = None
    extension_min_lead_days: int = 2
    extension_response_sla_hours: int = 48
    extension_penalty_threshold: float = 0.0


class RetailerResponse(RetailerRequest):
    """Response shape for a `retailer` row."""

    id: UUID


class RetailerLocationRequest(BaseModel):
    """Request body for creating or updating a retailer-owned location record."""

    location_code: str
    location_name: str | None = None
    location_type: str | None = Field(default=None, pattern="^(PLANT|DC|STORE|OTHER)$")
    address_line_1: str | None = None
    address_line_2: str | None = None
    city: str | None = None
    state_province: str | None = None
    postal_code: str | None = None
    country_code: str | None = None
    is_active: bool = True


class RetailerLocationResponse(RetailerLocationRequest):
    """Response shape for a `retailer_location` row."""

    id: UUID
    retailer_id: UUID


class SkuRequest(BaseModel):
    """Request body for creating/updating a SKU master-data record."""

    sku_code: str
    description: str | None = None
    material_id: UUID | None = None


class SkuResponse(SkuRequest):
    """Response shape for a `sku` row."""

    id: UUID


class MaterialRequest(BaseModel):
    """Request body for creating/updating a material master-data record."""

    material_code: str
    description: str | None = None


class MaterialResponse(MaterialRequest):
    """Response shape for a `material` row."""

    id: UUID


class MaterialMasterRequest(BaseModel):
    """Request body for creating or updating a plant-specific SAP material-master record."""

    material_id: UUID
    sap_material_number: str
    plant_id: UUID | None = None
    description: str | None = None
    available_quantity: float | None = None
    uom: str | None = None
    discontinuation_indicator: str | None = None
    effective_out_date: date | None = None
    follow_up_material_id: UUID | None = None
    source_system: str | None = None
    last_synced_at: datetime | None = None


class MaterialMasterResponse(MaterialMasterRequest):
    """Response shape for a `material_master` row."""

    id: UUID


class PlantRequest(BaseModel):
    """Request body for creating/updating a plant master-data record."""

    plant_code: str
    plant_name: str | None = None
    country_code: str | None = None


class PlantResponse(PlantRequest):
    """Response shape for a `plant` row."""

    id: UUID


class CarrierRequest(BaseModel):
    """Request body for creating/updating a carrier master-data record."""

    carrier_code: str
    carrier_name: str
    historical_reliability_score: float = Field(default=90.0, ge=0, le=100)


class CarrierResponse(CarrierRequest):
    """Response shape for a `carrier` row."""

    id: UUID
