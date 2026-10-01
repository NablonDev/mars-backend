"""Unit tests for `TimelineSimulator._complete_material_available`'s per-pool shortage check."""

from __future__ import annotations

from datetime import date

import pytest

from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.services.seeding.timeline.simulator import TimelineSimulator
from scripts.seed.seed_milestone_types import seed_milestone_types

T = date(2026, 6, 30)


@pytest.fixture
def simulator(db_session) -> TimelineSimulator:
    seed_milestone_types(db_session)
    return TimelineSimulator(db_session, T)


def _build_two_pool_plan(repos, timeline: FulfillmentTimelineRepository) -> dict:
    """A plan with two lines, each drawing from a different (material, plant) pool."""
    retailer = repos.master_data.add_retailer("RET-2POOL", "Retailer 2Pool", None, "SUM")
    plant_a = repos.master_data.add_plant("PLANT-A")
    plant_b = repos.master_data.add_plant("PLANT-B")
    material_a = repos.master_data.add_material("MAT-A")
    material_b = repos.master_data.add_material("MAT-B")
    repos.master_data.add_material_master(
        material_id=material_a["id"],
        sap_material_number="MAT-A",
        plant_id=plant_a["id"],
        available_quantity=0.0,
    )
    repos.master_data.add_material_master(
        material_id=material_b["id"],
        sap_material_number="MAT-B",
        plant_id=plant_b["id"],
        available_quantity=0.0,
    )
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-2POOL",
        retailer_id=retailer["id"],
        order_date=date(2026, 1, 1),
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
        cancel_date=None,
        freight_term="PREPAID",
    )
    line_a = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100.0,
        unit_price=20.0,
        material_id=material_a["id"],
        plant_id=plant_a["id"],
    )
    line_b = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="20",
        ordered_quantity=50.0,
        unit_price=20.0,
        material_id=material_b["id"],
        plant_id=plant_b["id"],
    )
    plan = timeline.create_plan(
        plan_number="PLAN-2POOL",
        purchase_order_id=purchase_order["id"],
        freight_term="PREPAID",
        planned_transit_days=2,
    )
    timeline.add_plan_line(plan["id"], line_a["id"], planned_quantity=100.0)
    timeline.add_plan_line(plan["id"], line_b["id"], planned_quantity=50.0)
    timeline.upsert_milestone(
        plan["id"], "MATERIAL_AVAILABLE", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 3)
    )
    return {
        "plan": plan,
        "material_a": material_a["id"],
        "plant_a": plant_a["id"],
        "material_b": material_b["id"],
        "plant_b": plant_b["id"],
    }


def test_two_pool_plan_completes_when_every_pool_is_fully_covered(repos, simulator: TimelineSimulator):
    scenario = _build_two_pool_plan(repos, simulator.timeline)
    simulator.timeline.adjust_available_quantity(scenario["material_a"], scenario["plant_a"], 100.0)
    simulator.timeline.adjust_available_quantity(scenario["material_b"], scenario["plant_b"], 50.0)
    milestone = {m["code"]: m for m in simulator.timeline.list_milestones(scenario["plan"]["id"])}[
        "MATERIAL_AVAILABLE"
    ]

    simulator._complete_material_available(
        scenario["plan"], milestone, date(2026, 1, 3), {"MATERIAL_AVAILABLE": milestone}
    )

    updated = {m["code"]: m for m in simulator.timeline.list_milestones(scenario["plan"]["id"])}[
        "MATERIAL_AVAILABLE"
    ]
    assert updated["actual_date"] == date(2026, 1, 3)
    assert updated["status"] == "DONE"


def test_two_pool_plan_with_a_shortage_in_one_pool_raises_not_implemented(
    repos, simulator: TimelineSimulator
):
    scenario = _build_two_pool_plan(repos, simulator.timeline)
    simulator.timeline.adjust_available_quantity(scenario["material_a"], scenario["plant_a"], 100.0)
    # Pool B is short: only 10 of the 50 units its line needs.
    simulator.timeline.adjust_available_quantity(scenario["material_b"], scenario["plant_b"], 10.0)
    milestone = {m["code"]: m for m in simulator.timeline.list_milestones(scenario["plan"]["id"])}[
        "MATERIAL_AVAILABLE"
    ]

    with pytest.raises(NotImplementedError):
        simulator._complete_material_available(
            scenario["plan"], milestone, date(2026, 1, 3), {"MATERIAL_AVAILABLE": milestone}
        )
