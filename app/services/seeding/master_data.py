"""Master/reference data seeds: retailers, materials/SKUs, plants, and carriers.

Shared by both projection and mitigation seeding, so it stays out of either
sub-domain's module. `material_master` and `warehouse` are deliberately not
seeded: no worked-example scenario reads them.
"""

from __future__ import annotations

import hashlib
from typing import TypedDict
from uuid import UUID

from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository


class _RetailerSeed(TypedDict):
    """One `retailer` row's seed fixture, matching `MasterDataRepository.add_retailer`'s fields."""

    retailer_code: str
    retailer_name: str
    priority_tier: str
    stacking_mode: str
    extension_min_lead_days: int
    extension_response_sla_hours: int
    extension_penalty_threshold: float


_RETAILERS: list[_RetailerSeed] = [
    {
        "retailer_code": "RET-WMT",
        "retailer_name": "Walmart",
        "priority_tier": "TIER_1",
        "stacking_mode": "SUM",
        "extension_min_lead_days": 2,
        "extension_response_sla_hours": 48,
        "extension_penalty_threshold": 200.0,
    },
    {
        "retailer_code": "RET-AMZ",
        "retailer_name": "Amazon",
        "priority_tier": "TIER_1",
        "stacking_mode": "SUM",
        # Amazon's shorter SLA is deliberate: it is what makes the AMZ-780112
        # timeout scenario expire within that order's own scenario window.
        "extension_min_lead_days": 2,
        "extension_response_sla_hours": 24,
        "extension_penalty_threshold": 100.0,
    },
]

# (material_code, sku_code, description)
_MATERIALS_AND_SKUS = [
    ("MAT-100234", "SKU-PED30", "Pedigree Adult Dry Dog Food 30lb"),
    ("MAT-100511", "SKU-CES12", "Cesar Adult Wet Dog Food Variety Pack 12ct"),
    ("MAT-100587", "SKU-WHI20", "Whiskas Adult Dry Cat Food 20lb"),
]

# (plant_code, plant_name); both the manufacturing plant and the
# distribution center are `plant` rows.
_PLANTS = [
    ("LOC-COL", "Mars Petcare Plant - Columbia MO"),
    ("LOC-ATL", "Mars DC - Atlanta GA"),
]


class _CarrierSeed(TypedDict):
    """One `carrier` row's seed fixture, matching `MasterDataRepository.add_carrier`'s fields."""

    carrier_code: str
    carrier_name: str
    historical_reliability_score: float


_CARRIERS: list[_CarrierSeed] = [
    {
        "carrier_code": "CAR-SWIFT",
        "carrier_name": "Swift Transportation",
        "historical_reliability_score": 92.0,
    },
    {"carrier_code": "CAR-JBHUNT", "carrier_name": "JB Hunt", "historical_reliability_score": 78.0},
]


def ensure_placeholder_retailer_agreement(
    retailer_agreements: RetailerAgreementRepository, retailer: dict
) -> UUID:
    """Idempotent per-retailer placeholder `retailer_agreement`, reused across seed runs.

    Seed/demo rules have no real uploaded contract to carry `penalty_rule.retailer_agreement_id`
    (NOT NULL); shared by projection, dispute, and this module's own retailer seeding.
    """
    existing = retailer_agreements.list_for_retailer(retailer["id"])
    if existing:
        return existing[0]["id"]
    created = retailer_agreements.add_retailer_agreement(
        retailer_id=retailer["id"],
        contract_code=f"SEED-{retailer['retailer_code']}",
        title=f"Seed placeholder agreement ({retailer['retailer_code']})",
        document_sha256=hashlib.sha256(f"seed-placeholder:{retailer['retailer_code']}".encode()).hexdigest(),
    )
    return created["id"]


def seed(
    master_data: MasterDataRepository, retailer_agreements: RetailerAgreementRepository
) -> dict[str, int]:
    """Seed master data, skipping any row whose natural key already exists.

    Also ensures a placeholder `retailer_agreement` per retailer:
    `penalty_rule.retailer_agreement_id` is NOT NULL, and worked-example rules have no
    genuine uploaded contract to point at.
    """
    counts = {"retailers": 0, "materials": 0, "skus": 0, "plants": 0, "carriers": 0}

    existing_retailers = {r["retailer_code"] for r in master_data.list_retailers()}
    for r in _RETAILERS:
        if r["retailer_code"] not in existing_retailers:
            master_data.add_retailer(
                r["retailer_code"],
                r["retailer_name"],
                r["priority_tier"],
                r["stacking_mode"],
                None,
                r["extension_min_lead_days"],
                r["extension_response_sla_hours"],
                r["extension_penalty_threshold"],
            )
            counts["retailers"] += 1
        retailer = master_data.get_retailer_by_code(r["retailer_code"])
        assert retailer is not None
        ensure_placeholder_retailer_agreement(retailer_agreements, retailer)

    existing_skus = {s["sku_code"] for s in master_data.list_skus()}
    for material_code, sku_code, description in _MATERIALS_AND_SKUS:
        material = master_data.get_material_by_code(material_code)
        if material is None:
            material = master_data.add_material(material_code, description)
            counts["materials"] += 1
        if sku_code not in existing_skus:
            master_data.add_sku(sku_code, description, material["id"])
            counts["skus"] += 1

    existing_plants = {p["plant_code"] for p in master_data.list_plants()}
    for plant_code, plant_name in _PLANTS:
        if plant_code not in existing_plants:
            master_data.add_plant(plant_code, plant_name)
            counts["plants"] += 1

    existing_carriers = {c["carrier_code"] for c in master_data.list_carriers()}
    for c in _CARRIERS:
        if c["carrier_code"] not in existing_carriers:
            master_data.add_carrier(c["carrier_code"], c["carrier_name"], c["historical_reliability_score"])
            counts["carriers"] += 1

    return counts
