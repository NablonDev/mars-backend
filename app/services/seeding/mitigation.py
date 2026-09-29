"""Mitigation-input seeds for the worked-example purchase orders."""

from __future__ import annotations

from typing import Any

from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.mitigation import MitigationInputRepository
from app.services.seeding.scenario_data_mitigation import SEEDED_MITIGATION_INPUTS


def _mitigation_to_seed_dict(inputs: Any) -> dict[str, Any]:
    """Convert a `MitigationInputs` fixture into `upsert_inputs` kwargs."""
    return {
        "purchase_order_number": inputs.order_id,
        "shortage_cause": inputs.shortage_cause.value,
        "shortage_cause_confirmed": inputs.shortage_cause_confirmed,
        "capacity_boost_cost_per_unit": inputs.capacity_boost_cost_per_unit,
        "capacity_boost_max_units_per_day": inputs.capacity_boost_max_units_per_day,
        "capacity_boost_data_confirmed": inputs.capacity_boost_data_confirmed,
        "express_carrier_cost": inputs.express_carrier_cost,
        "express_carrier_transit_days": inputs.express_carrier_transit_days,
        "express_carrier_data_confirmed": inputs.express_carrier_data_confirmed,
        "split_shipment_handling_cost": inputs.split_shipment_handling_cost,
    }


_MITIGATION_INPUTS = [_mitigation_to_seed_dict(inputs) for inputs in SEEDED_MITIGATION_INPUTS.values()]


def seed(
    mitigation_inputs: MitigationInputRepository, purchase_orders: PurchaseOrderRepository
) -> dict[str, int]:
    """Seed mitigation inputs, skipping purchase orders that already have a row."""
    counts = {"mitigation_inputs": 0}

    existing_purchase_order_ids = set(mitigation_inputs.list_purchase_order_ids())
    for m in _MITIGATION_INPUTS:
        purchase_order = purchase_orders.get_by_number(m["purchase_order_number"])
        if purchase_order is None:
            continue
        if purchase_order["id"] in existing_purchase_order_ids:
            continue

        fields = {k: v for k, v in m.items() if k != "purchase_order_number"}
        mitigation_inputs.upsert_inputs(purchase_order_id=purchase_order["id"], **fields)
        counts["mitigation_inputs"] += 1

    return counts
