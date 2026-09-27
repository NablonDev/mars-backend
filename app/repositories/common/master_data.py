"""Repository for `common` schema master data: retailers, SKUs, materials
(plus per-plant material_master), plants/storage locations/warehouses,
carriers, and retailer-owned ship-to locations.

Was `app/repositories/fine_master_data.py`. Every row now has a UUID
surrogate `id` (see app/db/base.py::generate_uuid7) in addition to its
natural business code (`retailer_code`, `sku_code`, ...); callers that
used to pass the business key straight into downstream FKs now look the
row up here first to get its `id`.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    return {"id": r.id, "sku_code": r.sku_code, "description": r.description, "material_id": r.material_id}


def _material_to_dict(r: Material) -> dict:
    return {"id": r.id, "material_code": r.material_code, "description": r.description}


def _material_master_to_dict(r: MaterialMaster) -> dict:
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
    return {
        "id": r.id,
        "plant_code": r.plant_code,
        "plant_name": r.plant_name,
        "country_code": r.country_code,
    }


def _storage_location_to_dict(r: StorageLocation) -> dict:
    return {
        "id": r.id,
        "plant_id": r.plant_id,
        "storage_location_code": r.storage_location_code,
        "storage_location_name": r.storage_location_name,
    }


def _warehouse_to_dict(r: Warehouse) -> dict:
    return {
        "id": r.id,
        "warehouse_code": r.warehouse_code,
        "warehouse_name": r.warehouse_name,
        "plant_id": r.plant_id,
    }


def _carrier_to_dict(r: Carrier) -> dict:
    return {
        "id": r.id,
        "carrier_code": r.carrier_code,
        "carrier_name": r.carrier_name,
        "historical_reliability_score": float(r.historical_reliability_score),
    }


def _retailer_location_to_dict(r: RetailerLocation) -> dict:
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
    def __init__(self, session: Session) -> None:
        self._session = session

    def _is_postgres(self) -> bool:
        return self._session.bind is not None and self._session.bind.dialect.name == "postgresql"

    # ------------------------------------------------------------------
    # Retailer
    # ------------------------------------------------------------------

    def _get_retailer_row(self, retailer_id: UUID) -> Retailer | None:
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

    def get_retailer_by_code(self, retailer_code: str) -> dict | None:
        row = self._session.scalars(select(Retailer).where(Retailer.retailer_code == retailer_code)).first()
        return _retailer_to_dict(row) if row is not None else None

    def get_or_create_retailer(
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
        """Atomic get-or-create for `common.retailer`, keyed on `retailer_code`
        (`ix_retailer_retailer_code`, unique) -- for PO-validation ingestion
        (`PoValidationService._ingest_one_line`), which needs "the row for
        this code, creating it on first sight" and must never raise on a
        concurrent creator winning the race.

        Distinct from `add_retailer` (unconditional insert, used by the
        direct `POST /retailers` admin route) -- that method and
        `get_retailer_by_code` are unchanged by this method's existence.

        Uses `INSERT ... ON CONFLICT (retailer_code) DO NOTHING` + a
        re-SELECT, the same precedent already used in
        `app.repositories.process.job_queue.JobQueueRepository.enqueue_many`.
        On a non-Postgres engine (the SQLite unit-test fixture), falls back
        to a plain check-then-insert -- safe only for a single-threaded
        caller, identical caveat to that existing precedent; the real
        concurrency guarantee is the Postgres unique index.
        """
        if self._is_postgres():
            stmt = pg_insert(Retailer).values(
                retailer_code=retailer_code,
                retailer_name=retailer_name,
                priority_tier=priority_tier,
                stacking_mode=stacking_mode,
                source_system=source_system,
                extension_min_lead_days=extension_min_lead_days,
                extension_response_sla_hours=extension_response_sla_hours,
                extension_penalty_threshold=extension_penalty_threshold,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["retailer_code"])
            self._session.execute(stmt)
            self._session.flush()
            return _retailer_to_dict(
                self._session.scalars(select(Retailer).where(Retailer.retailer_code == retailer_code)).one()
            )

        existing = self.get_retailer_by_code(retailer_code)
        if existing is not None:
            return existing
        return self.add_retailer(
            retailer_code,
            retailer_name,
            priority_tier,
            stacking_mode,
            source_system,
            extension_min_lead_days,
            extension_response_sla_hours,
            extension_penalty_threshold,
        )

    def list_retailers(self) -> list[dict]:
        rows = self._session.scalars(select(Retailer)).all()
        return [_retailer_to_dict(r) for r in rows]

    def get_stacking_mode(self, retailer_id: UUID) -> str:
        # retailer_id is a DB-level FK on every caller's table, so `retailer`
        # is never actually None here -- the fallback exists only because
        # nothing at the type level proves that to a caller of this method.
        retailer = self._get_retailer_row(retailer_id)
        return retailer.stacking_mode if retailer else "SUM"

    def get_extension_policy(self, retailer_id: UUID) -> dict:
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
        row = Sku(sku_code=sku_code, description=description, material_id=material_id)
        self._session.add(row)
        self._session.flush()
        return _sku_to_dict(row)

    def list_skus(self) -> list[dict]:
        rows = self._session.scalars(select(Sku)).all()
        return [_sku_to_dict(r) for r in rows]

    def add_material(self, material_code: str, description: str | None = None) -> dict:
        row = Material(material_code=material_code, description=description)
        self._session.add(row)
        self._session.flush()
        return _material_to_dict(row)

    def list_materials(self) -> list[dict]:
        """Phase 7a addition (flagged): `GET /api/v1/materials` (approved
        plan §5) had no listing method here -- purely additive, mirrors
        `list_skus`/`list_plants`/`list_carriers` above."""
        rows = self._session.scalars(select(Material)).all()
        return [_material_to_dict(r) for r in rows]

    def list_material_rows(self) -> list[Material]:
        """Every `Material`, as raw ORM objects -- for bulk consumers that
        need full attribute access (e.g. the ontology materialize job's
        `OntologyBuilder`), not the narrower dict shape `list_materials`
        returns for API responses."""
        return list(self._session.scalars(select(Material)).all())

    def get_material_by_code(self, material_code: str) -> dict | None:
        row = self._session.scalars(select(Material).where(Material.material_code == material_code)).first()
        return _material_to_dict(row) if row is not None else None

    def get_material_by_id(self, material_id: UUID) -> dict | None:
        """Reverse of `get_material_by_code` -- needed wherever only a FK
        value (e.g. `MaterialMaster.follow_up_material_id`) is on hand and
        the caller needs that Material's own natural key back for display,
        not just its id (see `app/agents/ontology_update/nodes.py`'s
        `build_proposal`, which shows a proposal's *current* value by
        material_code, not by opaque UUID)."""
        row = self._session.get(Material, material_id)
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
        """Phase 7a addition (flagged): `GET /api/v1/material-masters`
        (approved plan §5) had no listing method here -- purely additive."""
        rows = self._session.scalars(select(MaterialMaster)).all()
        return [_material_master_to_dict(r) for r in rows]

    def list_material_master_rows(self) -> list[MaterialMaster]:
        """Every `MaterialMaster`, as raw ORM objects -- see
        `list_material_rows`'s docstring for why this exists alongside the
        dict-returning `list_material_masters`."""
        return list(self._session.scalars(select(MaterialMaster)).all())

    def list_material_masters_for_material(self, material_id: UUID) -> list[dict]:
        """Every `MaterialMaster` row for one `Material`, across every plant
        it has stock at. `MaterialMaster` is one row per `(material_id,
        plant_id)` (`uq_material_master_material_plant`) -- a material with
        rows at more than one plant has no single unambiguous
        `MaterialMaster` to resolve to from the material code alone. This
        is exactly the read a caller needs to detect that ambiguity (return
        `clarification_required` rather than guessing a plant) before
        attempting any write; see `app/agents/ontology_update/nodes.py`."""
        rows = self._session.scalars(
            select(MaterialMaster).where(MaterialMaster.material_id == material_id)
        ).all()
        return [_material_master_to_dict(r) for r in rows]

    def update_material_master_follow_up(
        self, *, material_master_id: UUID, follow_up_material_id: UUID | None
    ) -> dict | None:
        """Set an existing `MaterialMaster` row's `follow_up_material_id` --
        the only write this repository has for `material_master` beyond the
        unconditional-insert `add_material_master`. Identifies the row by
        its own primary key (already resolved by the caller, e.g.
        `app.agents.ontology_update.nodes.resolve_target`, which is what
        disambiguates plant scoping *before* any write is attempted) rather
        than re-deriving it from `(material_id, plant_id)` here.

        Returns `None` if no such row exists (caller's job to treat that as
        an error, not this method's -- it stays a pure, honest "did this
        write happen" signal). `TimestampMixin.updated_at` bumps itself via
        the column's own `onupdate=func.now()`; nothing here sets it by hand.
        """
        row = self._session.get(MaterialMaster, material_master_id)
        if row is None:
            return None
        row.follow_up_material_id = follow_up_material_id
        self._session.flush()
        return _material_master_to_dict(row)

    def find_material_master(self, sap_material_number: str, plant_id: UUID) -> dict | None:
        row = self._session.scalars(
            select(MaterialMaster).where(
                MaterialMaster.sap_material_number == sap_material_number,
                MaterialMaster.plant_id == plant_id,
            )
        ).first()
        return _material_master_to_dict(row) if row is not None else None

    def find_material_master_by_material_id(self, material_id: UUID, plant_id: UUID) -> dict | None:
        """Resolve a `MaterialMaster.follow_up_material_id` (a `common.material.id`)
        to its real, plant-specific SAP number/description/available quantity --
        `uq_material_master_material_plant` makes `(material_id, plant_id)` unique,
        so this is a direct lookup, not a guess. Used by
        `app/agents/po_validation/nodes.py::human_qty_mismatch_decision` so the
        qty-mismatch candidate's suggested substitute is a real, submittable SAP
        material number instead of the raw `follow_up_material_id` UUID (which
        `_require_material` cannot match against any `sap_material_number` --
        see that call site's history for the bug this fixes, not just a display
        gap).

        Also joins `common.material` for `material_code` -- the business-facing
        code (distinct from `sap_material_number`, which is per-plant/logistics)
        -- so the UI can show the substitute's real material code, not just its
        SAP number."""
        row = self._session.execute(
            select(MaterialMaster, Material.material_code)
            .join(Material, Material.id == MaterialMaster.material_id)
            .where(
                MaterialMaster.material_id == material_id,
                MaterialMaster.plant_id == plant_id,
            )
        ).first()
        if row is None:
            return None
        master_row, material_code = row
        result = _material_master_to_dict(master_row)
        result["material_code"] = material_code
        return result

    # ------------------------------------------------------------------
    # Plant / StorageLocation / Warehouse
    # ------------------------------------------------------------------

    def add_plant(
        self, plant_code: str, plant_name: str | None = None, country_code: str | None = None
    ) -> dict:
        row = Plant(plant_code=plant_code, plant_name=plant_name, country_code=country_code)
        self._session.add(row)
        self._session.flush()
        return _plant_to_dict(row)

    def get_plant_by_code(self, plant_code: str) -> dict | None:
        row = self._session.scalars(select(Plant).where(Plant.plant_code == plant_code)).first()
        return _plant_to_dict(row) if row is not None else None

    def get_or_create_plant(
        self, plant_code: str, plant_name: str | None = None, country_code: str | None = None
    ) -> dict:
        """Atomic get-or-create for `common.plant`, keyed on `plant_code`
        (`ix_plant_plant_code`, unique) -- see `get_or_create_retailer`'s
        docstring for the full rationale/precedent; `add_plant`/
        `get_plant_by_code` are unchanged."""
        if self._is_postgres():
            stmt = pg_insert(Plant).values(
                plant_code=plant_code, plant_name=plant_name, country_code=country_code
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["plant_code"])
            self._session.execute(stmt)
            self._session.flush()
            return _plant_to_dict(
                self._session.scalars(select(Plant).where(Plant.plant_code == plant_code)).one()
            )

        existing = self.get_plant_by_code(plant_code)
        if existing is not None:
            return existing
        return self.add_plant(plant_code, plant_name, country_code)

    def list_plants(self) -> list[dict]:
        rows = self._session.scalars(select(Plant)).all()
        return [_plant_to_dict(r) for r in rows]

    def list_plant_rows(self) -> list[Plant]:
        """Every `Plant`, as raw ORM objects -- see `list_material_rows`'s
        docstring for why this exists alongside the dict-returning
        `list_plants`."""
        return list(self._session.scalars(select(Plant)).all())

    def add_storage_location(
        self, plant_id: UUID, storage_location_code: str, storage_location_name: str | None = None
    ) -> dict:
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
        row = Carrier(
            carrier_code=carrier_code,
            carrier_name=carrier_name,
            historical_reliability_score=historical_reliability_score,
        )
        self._session.add(row)
        self._session.flush()
        return _carrier_to_dict(row)

    def get_carrier(self, carrier_id: UUID) -> dict | None:
        row = self._session.get(Carrier, carrier_id)
        return _carrier_to_dict(row) if row is not None else None

    def list_carriers(self) -> list[dict]:
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
        rows = self._session.scalars(
            select(RetailerLocation).where(RetailerLocation.retailer_id == retailer_id)
        ).all()
        return [_retailer_location_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def truncate_all(self) -> None:
        """Deletes every master-data row, for a force-reseed, in FK-safe
        child-before-parent order. Caller must first clear anything that
        FK-references these (penalty_rule, purchase_order, and
        purchase_order's own dependents) -- see the seeding service for
        the full order."""
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
