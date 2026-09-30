"""Idempotent master-data setup for the fulfillment-timeline scenario simulator.

Get-or-create for retailers, their agreements and penalty rules, plants, and
materials/material masters -- all `TL-`-prefixed so `TimelineSimulator.reset`
can remove exactly what this module creates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.utils.hashing import content_sha256

RULE_DEFAULT_START = date(2020, 1, 1)
QA_RELEASE_DAYS = 3

MATERIALS: dict[str, tuple[str, float]] = {
    "TL-MAT-PEDIGREE": ("Pedigree 30 lb", 19.50),
    "TL-MAT-TEMPTATIONS": ("Temptations", 18.00),
    "TL-MAT-ROYALCANIN": ("Royal Canin", 55.00),
    "TL-MAT-GREENIES": ("Greenies", 28.00),
    "TL-MAT-CESAR": ("Cesar Canine", 22.00),
    "TL-MAT-SHEBA": ("Sheba Wet Cat", 24.00),
}
PLANTS: dict[str, str] = {"TL-JOP": "Joplin", "TL-COL": "Columbus"}
CARRIERS: tuple[str, ...] = ("TL-CARRIER-1", "TL-CARRIER-2")

_AMAZON_RULES = (
    {
        "rule_code": "TL-RULE-AMZ-OTIF",
        "violation_type": "OTIF_LATE",
        "calc_type": "PERCENT_OF_PO",
        "rate": 0.03,
        "penalty_category": "LATE",
        "basis_type": "COST_OF_GOODS",
    },
    {
        "rule_code": "TL-RULE-AMZ-DWV",
        "violation_type": "DELIVERY_WINDOW_VIOLATION",
        "calc_type": "PERCENT_OF_PO",
        "rate": 0.03,
        "penalty_category": "EARLY",
    },
    {
        "rule_code": "TL-RULE-AMZ-SHORT",
        "violation_type": "SHORT_SHIP",
        "calc_type": "PERCENT_OF_PO",
        "rate": 0.03,
        "penalty_category": "SHORT",
        "basis_type": "SHORTFALL_VALUE",
        "threshold_pct": 0.0,
    },
    {
        "rule_code": "TL-RULE-AMZ-ASN",
        "violation_type": "ASN_LATE",
        "calc_type": "FLAT_FEE",
        "rate": 250.0,
        "penalty_category": "ASN_LATE",
    },
)
_WALMART_RULES = (
    {
        "rule_code": "TL-RULE-WMT-OTIF",
        "violation_type": "OTIF_LATE",
        "calc_type": "PERCENT_OF_PO",
        "rate": 0.03,
        "penalty_category": "LATE",
    },
    {
        "rule_code": "TL-RULE-WMT-DWV",
        "violation_type": "DELIVERY_WINDOW_VIOLATION",
        "calc_type": "PERCENT_OF_PO",
        "rate": 0.03,
        "penalty_category": "EARLY",
    },
    {
        "rule_code": "TL-RULE-WMT-SHORT",
        "violation_type": "SHORT_SHIP",
        "calc_type": "PERCENT_OF_PO",
        "rate": 0.03,
        "penalty_category": "SHORT",
        "basis_type": "SHORTFALL_VALUE",
        "threshold_pct": 0.98,
    },
)
_COSTCO_RULES = (
    {
        "rule_code": "TL-RULE-CST-OTIF",
        "violation_type": "OTIF_LATE",
        "calc_type": "PERCENT_OF_PO",
        "rate": 0.02,
        "penalty_category": "LATE",
        "grace_period_days": 1,
    },
    {
        "rule_code": "TL-RULE-CST-SHORT",
        "violation_type": "SHORT_SHIP",
        "calc_type": "PERCENT_OF_PO",
        "rate": 0.02,
        "penalty_category": "SHORT",
        "basis_type": "SHORTFALL_VALUE",
        "threshold_pct": 0.98,
    },
)
_TARGET_RULES = (
    {
        "rule_code": "TL-RULE-TGT-OTIF",
        "violation_type": "OTIF_LATE",
        "calc_type": "PERCENT_OF_PO",
        "rate": 0.03,
        "penalty_category": "LATE",
    },
    {
        "rule_code": "TL-RULE-TGT-DWV",
        "violation_type": "DELIVERY_WINDOW_VIOLATION",
        "calc_type": "PERCENT_OF_PO",
        "rate": 0.03,
        "penalty_category": "EARLY",
    },
    {
        "rule_code": "TL-RULE-TGT-SHORT",
        "violation_type": "SHORT_SHIP",
        "calc_type": "PERCENT_OF_PO",
        "rate": 0.03,
        "penalty_category": "SHORT",
        "basis_type": "SHORTFALL_VALUE",
        "threshold_pct": 0.98,
    },
)
RETAILERS: dict[str, tuple[str, tuple[dict, ...]]] = {
    "TL-AMAZON": ("Amazon", _AMAZON_RULES),
    "TL-WALMART": ("Walmart", _WALMART_RULES),
    "TL-COSTCO": ("Costco", _COSTCO_RULES),
    "TL-TARGET": ("Target", _TARGET_RULES),
}


@dataclass
class TimelineMasterData:
    """Owns get-or-create master data for the simulator, and the id lookups it exposes."""

    master_data: MasterDataRepository
    retailer_agreements: RetailerAgreementRepository
    rules: PenaltyRuleRepository
    retailer_ids: dict[str, UUID] = field(default_factory=dict)
    plant_ids: dict[str, UUID] = field(default_factory=dict)
    material_ids: dict[str, UUID] = field(default_factory=dict)

    def setup(self, as_of_date: date) -> None:
        """Get-or-create every retailer/agreement/rule, plant, material, and carrier.

        A retailer reused by name that already carries active, engine-priceable
        rules effective on `as_of_date` on its own (non-seeder) agreement keeps
        that agreement untouched and gets no synthetic rules attached to it; the
        scenarios are then priced by its real contract instead.
        """
        for retailer_code, (retailer_name, rule_specs) in RETAILERS.items():
            retailer = self.master_data.get_retailer_by_name(retailer_name)
            if retailer is None:
                retailer = self.master_data.get_retailer_by_code(retailer_code)
            if retailer is None:
                retailer = self.master_data.add_retailer(retailer_code, retailer_name, "TIER_1", "SUM")
            self.retailer_ids[retailer_code] = retailer["id"]

            agreement_id = self._get_or_create_agreement(retailer["id"], retailer_code)
            if self._has_real_contract_rules(retailer["id"], agreement_id, as_of_date):
                print(
                    f"{retailer_code}: retailer already priced by a real contract's "
                    "engine-priceable rules; skipping synthetic rule creation."
                )
                continue
            self._get_or_create_rules(agreement_id, rule_specs)

        for plant_code, plant_name in PLANTS.items():
            plant = self.master_data.get_plant_by_code(plant_code)
            if plant is None:
                plant = self.master_data.add_plant(plant_code, plant_name)
            self.plant_ids[plant_code] = plant["id"]

        for material_code, (description, standard_cost) in MATERIALS.items():
            self._get_or_create_material(material_code, description, standard_cost)

        existing_carrier_codes = {c["carrier_code"] for c in self.master_data.list_carriers()}
        for carrier_code in CARRIERS:
            if carrier_code not in existing_carrier_codes:
                self.master_data.add_carrier(carrier_code, carrier_code)

    def _get_or_create_agreement(self, retailer_id: UUID, retailer_code: str) -> UUID:
        """Get-or-create the one seeder-owned retailer agreement, identified by its own contract code.

        Never reuses `list_for_retailer(retailer_id)[0]`: for a retailer reused
        by name, that could be someone else's real contract, not this seeder's
        agreement.
        """
        contract_code = f"{retailer_code}-AGREEMENT"
        existing = self.retailer_agreements.get_by_contract_code(contract_code)
        if existing is not None:
            return existing["id"]

        document_sha256 = content_sha256(f"Synthetic timeline demo agreement: {contract_code}")
        duplicate = self.retailer_agreements.get_by_document_sha256(document_sha256)
        if duplicate is not None:
            raise ValueError(
                f"Seeder agreement {contract_code!r} would collide on document_sha256 with "
                f"existing agreement {duplicate['contract_code']!r}."
            )

        created = self.retailer_agreements.add_retailer_agreement(
            retailer_id=retailer_id,
            contract_code=contract_code,
            title=f"{retailer_code} synthetic agreement",
            document_sha256=document_sha256,
            effective_date=RULE_DEFAULT_START,
        )
        return created["id"]

    def _has_real_contract_rules(
        self, retailer_id: UUID, seeder_agreement_id: UUID, as_of_date: date
    ) -> bool:
        """True when the retailer has active, engine-priceable rules on a non-seeder agreement."""
        rules = self.rules.list_rules_effective_on(retailer_id, as_of_date)
        return any(
            rule["is_active"]
            and rule["is_engine_priceable"]
            and rule["retailer_agreement_id"] != seeder_agreement_id
            for rule in rules
        )

    def _get_or_create_rules(self, agreement_id: UUID, rule_specs: tuple[dict, ...]) -> None:
        for spec in rule_specs:
            if self.rules.get_by_rule_code(spec["rule_code"]) is not None:
                continue
            self.rules.add_rule(
                rule_code=spec["rule_code"],
                violation_type=spec["violation_type"],
                calc_type=spec["calc_type"],
                rate=spec["rate"],
                penalty_category=spec["penalty_category"],
                retailer_agreement_id=agreement_id,
                basis_type=spec.get("basis_type"),
                threshold_pct=spec.get("threshold_pct", 0.0),
                grace_period_days=spec.get("grace_period_days", 0),
                effective_start_date=RULE_DEFAULT_START,
            )

    def _get_or_create_material(self, material_code: str, description: str, standard_cost: float) -> None:
        material = self.master_data.get_material_by_code(material_code)
        if material is None:
            material = self.master_data.add_material(material_code, description)
        self.material_ids[material_code] = material["id"]

        for plant_id in self.plant_ids.values():
            if self.master_data.find_material_master(material_code, plant_id) is None:
                self.master_data.add_material_master(
                    material_id=material["id"],
                    sap_material_number=material_code,
                    plant_id=plant_id,
                    available_quantity=0.0,
                    standard_cost=standard_cost,
                    qa_release_days=QA_RELEASE_DAYS,
                )
