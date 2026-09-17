"""Repository for common schema master data: retailers, SKUs, materials, plants, carriers."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    Carrier,
    Material,
    MaterialMaster,
    Plant,
    Retailer,
    RetailerLocation,
    Sku,
    StorageLocation,
    Warehouse,
)


def _retailer_to_dict(r: Retailer) -> dict:
    """Serialize a Retailer row into a dict."""
    return {
        "id": r.id,
        "retailer_code": r.retailer_code,
        "retailer_name": r.retailer_name,
        "priority_tier": r.priority_tier,
        "stacking_mode": r.stacking_mode,
        "source_system": r.source_system,
        "extension_min_lead_days": r.extension_min_lead_days,
        "extension_response_sla_hours": r.extension_response_sla_hours,
        "extension_penalty_threshold": float(r.extension_penalty_threshold),
    }


def _sku_to_dict(r: Sku) -> dict:
    """Serialize a Sku row into a dict."""
    return {"id": r.id, "sku_code": r.sku_code, "description": r.description, "material_id": r.material_id}


def _material_to_dict(r: Material) -> dict:
    """Serialize a Material row into a dict."""
    return {"id": r.id, "material_code": r.material_code, "description": r.description}


def _material_master_to_dict(r: MaterialMaster) -> dict:
    """Serialize a MaterialMaster row into a dict."""
    return {
        "id": r.id,
        "material_id": r.material_id,
        "sap_material_number": r.sap_material_number,
        "plant_id": r.plant_id,
        "description": r.description,
        "available_quantity": float(r.available_quantity) if r.available_quantity is not None else None,
        "uom": r.uom,
        "discontinuation_indicator": r.discontinuation_indicator,
        "effective_out_date": r.effective_out_date,
        "follow_up_material_id": r.follow_up_material_id,
        "source_system": r.source_system,
        "last_synced_at": r.last_synced_at,
    }


def _plant_to_dict(r: Plant) -> dict:
    """Serialize a Plant row into a dict."""
    return {
        "id": r.id,
        "plant_code": r.plant_code,
        "plant_name": r.plant_name,
        "country_code": r.country_code,
    }


def _storage_location_to_dict(r: StorageLocation) -> dict:
    """Serialize a StorageLocation row into a dict."""
    return {
        "id": r.id,
        "plant_id": r.plant_id,
        "storage_location_code": r.storage_location_code,
        "storage_location_name": r.storage_location_name,
    }


def _warehouse_to_dict(r: Warehouse) -> dict:
    """Serialize a Warehouse row into a dict."""
    return {
        "id": r.id,
        "warehouse_code": r.warehouse_code,
        "warehouse_name": r.warehouse_name,
        "plant_id": r.plant_id,
    }


def _carrier_to_dict(r: Carrier) -> dict:
    """Serialize a Carrier row into a dict."""
    return {
        "id": r.id,
        "carrier_code": r.carrier_code,
        "carrier_name": r.carrier_name,
        "historical_reliability_score": float(r.historical_reliability_score),
    }


def _retailer_location_to_dict(r: RetailerLocation) -> dict:
    """Serialize a RetailerLocation row into a dict."""
    return {
        "id": r.id,
        "retailer_id": r.retailer_id,
        "location_code": r.location_code,
        "location_name": r.location_name,
        "location_type": r.location_type,
        "address_line_1": r.address_line_1,
        "address_line_2": r.address_line_2,
        "city": r.city,
        "state_province": r.state_province,
        "postal_code": r.postal_code,
        "country_code": r.country_code,
        "is_active": r.is_active,
    }


class MasterDataRepository:
    """Access layer for common schema master data.

    Manages retailers, SKUs, materials, plants, carriers, warehouses, and
    retailer locations. All writes are idempotent (upsert on natural keys).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Retailer
    # ------------------------------------------------------------------

    def _get_retailer_row(self, retailer_id: UUID) -> Retailer | None:
        """Fetch a retailer row by its surrogate id, or None if not found."""
        return self._session.get(Retailer, retailer_id)

    def add_retailer(
        self,
        retailer_code: str,
        retailer_name: str,
        priority_tier: str | None,
        stacking_mode: str = "SUM",
        source_system: str | None = None,
        extension_min_lead_days: int = 2,
        extension_response_sla_hours: int = 48,
        extension_penalty_threshold: float = 0.0,
    ) -> dict:
        """Create a retailer row and return it as a dict.

        `stacking_mode` and the `extension_*` fields seed the retailer's
        penalty-stacking and delivery-change-request policies with sane
        defaults, so callers only need to override the ones a given
        retailer actually differs on.
        """
        row = Retailer(
            retailer_code=retailer_code,
            retailer_name=retailer_name,
            priority_tier=priority_tier,
            stacking_mode=stacking_mode,
            source_system=source_system,
            extension_min_lead_days=extension_min_lead_days,
            extension_response_sla_hours=extension_response_sla_hours,
            extension_penalty_threshold=extension_penalty_threshold,
        )
        self._session.add(row)
        self._session.flush()
        return _retailer_to_dict(row)

    def get_retailer(self, retailer_id: UUID) -> dict | None:
        """Fetch a retailer by its surrogate id, or None if not found."""
        row = self._get_retailer_row(retailer_id)
        return _retailer_to_dict(row) if row is not None else None

    def get_retailer_by_code(self, retailer_code: str) -> dict | None:
        """Fetch a retailer by its natural key `retailer_code`, or None if not found."""
        row = self._session.scalars(select(Retailer).where(Retailer.retailer_code == retailer_code)).first()
        return _retailer_to_dict(row) if row is not None else None

    def list_retailers(self) -> list[dict]:
        """List every retailer, in no guaranteed order."""
        rows = self._session.scalars(select(Retailer)).all()
        return [_retailer_to_dict(r) for r in rows]

    def get_stacking_mode(self, retailer_id: UUID) -> str:
        """Fetch a retailer's penalty stacking mode (SUM or MAX), defaulting to SUM."""
        # retailer_id is a DB-level FK on every caller's table, so the row always
        # exists; the fallback is here only because nothing at the type level says so.
        retailer = self._get_retailer_row(retailer_id)
        return retailer.stacking_mode if retailer else "SUM"

    def get_extension_policy(self, retailer_id: UUID) -> dict:
        """Fetch extension (DCR) policy for a retailer, with built-in defaults."""
        retailer = self._get_retailer_row(retailer_id)
        if retailer is None:
            return {"min_lead_days": 2, "response_sla_hours": 48, "penalty_threshold": 0.0}
        return {
            "min_lead_days": retailer.extension_min_lead_days,
            "response_sla_hours": retailer.extension_response_sla_hours,
            "penalty_threshold": float(retailer.extension_penalty_threshold),
        }

    # ------------------------------------------------------------------
    # Sku / Material / MaterialMaster
    # ------------------------------------------------------------------

    def add_sku(self, sku_code: str, description: str | None = None, material_id: UUID | None = None) -> dict:
        """Create a SKU row; `material_id` stays None until the SKU is mapped to a material."""
        row = Sku(sku_code=sku_code, description=description, material_id=material_id)
        self._session.add(row)
        self._session.flush()
        return _sku_to_dict(row)

    def list_skus(self) -> list[dict]:
        """List every SKU, in no guaranteed order."""
        rows = self._session.scalars(select(Sku)).all()
        return [_sku_to_dict(r) for r in rows]

    def add_material(self, material_code: str, description: str | None = None) -> dict:
        """Create a material row and return it as a dict.

        `Material` is the plant-agnostic product definition; per-plant
        inventory and sourcing facts live on `MaterialMaster` instead.
        """
        row = Material(material_code=material_code, description=description)
        self._session.add(row)
        self._session.flush()
        return _material_to_dict(row)

    def list_materials(self) -> list[dict]:
        """List every material, in no guaranteed order."""
        rows = self._session.scalars(select(Material)).all()
        return [_material_to_dict(r) for r in rows]

    def get_material_by_code(self, material_code: str) -> dict | None:
        """Fetch a material by its natural key `material_code`, or None if not found."""
        row = self._session.scalars(select(Material).where(Material.material_code == material_code)).first()
        return _material_to_dict(row) if row is not None else None

    def add_material_master(
        self,
        material_id: UUID,
        sap_material_number: str,
        plant_id: UUID | None = None,
        description: str | None = None,
        available_quantity: float | None = None,
        uom: str | None = None,
        discontinuation_indicator: str | None = None,
        effective_out_date: date | None = None,
        follow_up_material_id: UUID | None = None,
        source_system: str | None = None,
        last_synced_at: datetime | None = None,
    ) -> dict:
        """Create a material_master row and return it as a dict.

        Holds the per-plant inventory and lifecycle facts for a material: available
        quantity, discontinuation status, and the `follow_up_material_id` a purchase
        order should be re-sourced against once the material is phased out.
        `find_material_master` is the natural-key lookup on (`sap_material_number`,
        `plant_id`).
        """
        row = MaterialMaster(
            material_id=material_id,
            sap_material_number=sap_material_number,
            plant_id=plant_id,
            description=description,
            available_quantity=available_quantity,
            uom=uom,
            discontinuation_indicator=discontinuation_indicator,
            effective_out_date=effective_out_date,
            follow_up_material_id=follow_up_material_id,
            source_system=source_system,
            last_synced_at=last_synced_at,
        )
        self._session.add(row)
        self._session.flush()
        return _material_master_to_dict(row)

    def list_material_masters(self) -> list[dict]:
        """List every material_master row across all materials and plants."""
        rows = self._session.scalars(select(MaterialMaster)).all()
        return [_material_master_to_dict(r) for r in rows]

    def find_material_master(self, sap_material_number: str, plant_id: UUID) -> dict | None:
        """Fetch a material_master by SAP number and plant, or None if not found."""
        row = self._session.scalars(
            select(MaterialMaster).where(
                MaterialMaster.sap_material_number == sap_material_number,
                MaterialMaster.plant_id == plant_id,
            )
        ).first()
        return _material_master_to_dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Plant / StorageLocation / Warehouse
    # ------------------------------------------------------------------

    def add_plant(
        self, plant_code: str, plant_name: str | None = None, country_code: str | None = None
    ) -> dict:
        """Create a plant row, identified by `plant_code`."""
        row = Plant(plant_code=plant_code, plant_name=plant_name, country_code=country_code)
        self._session.add(row)
        self._session.flush()
        return _plant_to_dict(row)

    def get_plant_by_code(self, plant_code: str) -> dict | None:
        """Fetch a plant by its natural key `plant_code`, or None if not found."""
        row = self._session.scalars(select(Plant).where(Plant.plant_code == plant_code)).first()
        return _plant_to_dict(row) if row is not None else None

    def list_plants(self) -> list[dict]:
        """List every plant, in no guaranteed order."""
        rows = self._session.scalars(select(Plant)).all()
        return [_plant_to_dict(r) for r in rows]

    def add_storage_location(
        self, plant_id: UUID, storage_location_code: str, storage_location_name: str | None = None
    ) -> dict:
        """Create a storage_location row, always scoped to a single plant."""
        row = StorageLocation(
            plant_id=plant_id,
            storage_location_code=storage_location_code,
            storage_location_name=storage_location_name,
        )
        self._session.add(row)
        self._session.flush()
        return _storage_location_to_dict(row)

    def add_warehouse(
        self, warehouse_code: str, warehouse_name: str | None = None, plant_id: UUID | None = None
    ) -> dict:
        """Create a warehouse row; a warehouse need not belong to any plant."""
        row = Warehouse(warehouse_code=warehouse_code, warehouse_name=warehouse_name, plant_id=plant_id)
        self._session.add(row)
        self._session.flush()
        return _warehouse_to_dict(row)

    # ------------------------------------------------------------------
    # Carrier
    # ------------------------------------------------------------------

    def add_carrier(
        self, carrier_code: str, carrier_name: str, historical_reliability_score: float = 90.0
    ) -> dict:
        """Create a carrier row and return it as a dict.

        `historical_reliability_score` defaults to a neutral 90.0 when the
        real historical figure for a new carrier isn't known yet.
        """
        row = Carrier(
            carrier_code=carrier_code,
            carrier_name=carrier_name,
            historical_reliability_score=historical_reliability_score,
        )
        self._session.add(row)
        self._session.flush()
        return _carrier_to_dict(row)

    def get_carrier(self, carrier_id: UUID) -> dict | None:
        """Fetch a carrier by surrogate id, or None if not found."""
        row = self._session.get(Carrier, carrier_id)
        return _carrier_to_dict(row) if row is not None else None

    def list_carriers(self) -> list[dict]:
        """List every carrier, in no guaranteed order."""
        rows = self._session.scalars(select(Carrier)).all()
        return [_carrier_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # RetailerLocation
    # ------------------------------------------------------------------

    def add_retailer_location(
        self,
        retailer_id: UUID,
        location_code: str,
        location_name: str | None = None,
        location_type: str | None = None,
        address_line_1: str | None = None,
        address_line_2: str | None = None,
        city: str | None = None,
        state_province: str | None = None,
        postal_code: str | None = None,
        country_code: str | None = None,
        is_active: bool = True,
    ) -> dict:
        """Create a retailer_location row and return it as a dict.

        `location_code` is the retailer-scoped natural key; every address field is
        optional. Locations are retired by setting `is_active` false, not deleted.
        """
        row = RetailerLocation(
            retailer_id=retailer_id,
            location_code=location_code,
            location_name=location_name,
            location_type=location_type,
            address_line_1=address_line_1,
            address_line_2=address_line_2,
            city=city,
            state_province=state_province,
            postal_code=postal_code,
            country_code=country_code,
            is_active=is_active,
        )
        self._session.add(row)
        self._session.flush()
        return _retailer_location_to_dict(row)

    def list_retailer_locations(self, retailer_id: UUID) -> list[dict]:
        """List every location for a retailer, inactive ones included."""
        rows = self._session.scalars(
            select(RetailerLocation).where(RetailerLocation.retailer_id == retailer_id)
        ).all()
        return [_retailer_location_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def truncate_all(self) -> None:
        """Delete every master-data row, child before parent, for a force-reseed.

        Callers must first clear everything that FK-references master data
        (penalty_rule, purchase_order and its dependents); the seeding service owns
        that order.
        """
        self._session.execute(delete(RetailerLocation))
        self._session.execute(delete(Warehouse))
        self._session.execute(delete(StorageLocation))
        self._session.execute(delete(MaterialMaster))
        self._session.execute(delete(Sku))
        self._session.execute(delete(Material))
        self._session.execute(delete(Carrier))
        self._session.execute(delete(Plant))
        self._session.execute(delete(Retailer))
        self._session.flush()
