"""Tests for `FulfillmentTimelineRepository`."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import update

from app.models import FulfillmentPlan, MaterialMaster, ProductionOrder, QualityLot
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from scripts.seed.seed_milestone_types import MILESTONE_TYPE_SEEDS, seed_milestone_types


def _seed_plan(repos, timeline: FulfillmentTimelineRepository, plan_number: str = "PLAN-1") -> dict:
    retailer = repos.master_data.add_retailer(f"RET-{plan_number}", "Retailer", None, "SUM")
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=f"PO-{plan_number}", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    return timeline.create_plan(
        plan_number=plan_number, purchase_order_id=purchase_order["id"], freight_term="PREPAID"
    )


def test_list_milestone_definitions_returns_all_seeded_milestones(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    seed_milestone_types(db_session)

    definitions = timeline.list_milestone_definitions()

    assert len(definitions) == len(MILESTONE_TYPE_SEEDS)
    assert {d.code for d in definitions} == {s.code for s in MILESTONE_TYPE_SEEDS}
    delivered = next(d for d in definitions if d.code == "DELIVERED")
    assert delivered.is_measurement_point is True
    assert delivered.default_duration_days is None


def test_create_plan_is_idempotent_on_plan_number(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    plan = _seed_plan(repos, timeline)

    again = timeline.create_plan(
        plan_number=plan["plan_number"], purchase_order_id=plan["purchase_order_id"], freight_term="COLLECT"
    )

    assert again["id"] == plan["id"]
    assert again["freight_term"] == "PREPAID"


def test_add_plan_line_is_idempotent_on_plan_and_po_line(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    plan = _seed_plan(repos, timeline)
    po_line = repos.purchase_orders.add_line(
        purchase_order_id=plan["purchase_order_id"], line_number="10", ordered_quantity=100, unit_price=5.0
    )

    line = timeline.add_plan_line(plan["id"], po_line["id"], planned_quantity=100)
    again = timeline.add_plan_line(plan["id"], po_line["id"], planned_quantity=999)

    assert again["id"] == line["id"]
    assert again["planned_quantity"] == 100


def test_upsert_milestone_sets_baseline_once_and_updates_other_fields(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    seed_milestone_types(db_session)
    plan = _seed_plan(repos, timeline)

    first = timeline.upsert_milestone(
        plan["id"], "PICKED", baseline_date=date(2026, 1, 5), planned_date=date(2026, 1, 5), status="PENDING"
    )
    assert first["baseline_date"] == date(2026, 1, 5)

    updated = timeline.upsert_milestone(
        plan["id"], "PICKED", baseline_date=date(2026, 1, 9), planned_date=date(2026, 1, 8), status="DONE"
    )

    assert updated["id"] == first["id"]
    assert updated["baseline_date"] == date(2026, 1, 5)
    assert updated["planned_date"] == date(2026, 1, 8)
    assert updated["status"] == "DONE"


def test_list_milestones_includes_code(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    seed_milestone_types(db_session)
    plan = _seed_plan(repos, timeline)
    timeline.upsert_milestone(plan["id"], "ORDER_RECEIVED", baseline_date=date(2026, 1, 1))

    milestones = timeline.list_milestones(plan["id"])

    assert [m["code"] for m in milestones] == ["ORDER_RECEIVED"]
    assert milestones[0]["actual_date"] is None


def test_add_event_and_list_events_and_latest_event_for_milestone(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    seed_milestone_types(db_session)
    plan = _seed_plan(repos, timeline)
    milestone = timeline.upsert_milestone(plan["id"], "PICKED", baseline_date=date(2026, 1, 5))

    timeline.add_event(
        subject_type="PLAN_MILESTONE",
        subject_id=milestone["id"],
        fulfillment_plan_id=plan["id"],
        milestone_type_id=milestone["milestone_type_id"],
        event_type="PLAN_CHANGED",
        old_value="2026-01-05",
        new_value="2026-01-07",
        reason_code="CARRIER_DELAY",
        event_at=datetime(2026, 1, 2, tzinfo=UTC),
        source="SIMULATOR",
    )
    timeline.add_event(
        subject_type="PLAN_MILESTONE",
        subject_id=milestone["id"],
        fulfillment_plan_id=plan["id"],
        milestone_type_id=milestone["milestone_type_id"],
        event_type="PLAN_CHANGED",
        old_value="2026-01-07",
        new_value="2026-01-09",
        reason_code="WEATHER",
        event_at=datetime(2026, 1, 3, tzinfo=UTC),
        source="SIMULATOR",
    )

    events = timeline.list_events(plan["id"])
    assert len(events) == 2
    assert events[0]["reason_code"] == "CARRIER_DELAY"

    latest = timeline.latest_event_for_milestone(plan["id"], "PICKED")
    assert latest is not None
    assert latest["reason_code"] == "WEATHER"

    timeline.add_event(
        subject_type="PLAN_MILESTONE",
        subject_id=milestone["id"],
        fulfillment_plan_id=plan["id"],
        milestone_type_id=milestone["milestone_type_id"],
        event_type="COMPLETED",
        old_value="2026-01-09",
        new_value="2026-01-09",
        reason_code=None,
        event_at=datetime(2026, 1, 4, tzinfo=UTC),
        source="SIMULATOR",
    )

    assert timeline.latest_event_for_milestone(plan["id"], "PICKED")["reason_code"] is None
    reasoned = timeline.latest_reasoned_event_for_milestone(plan["id"], "PICKED")
    assert reasoned is not None
    assert reasoned["reason_code"] == "WEATHER"


def test_list_events_for_subject_and_latest_event_for_subject(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    production_order = ProductionOrder(production_order_number="PO-EVT-1", status="IN_PROGRESS")
    db_session.add(production_order)
    db_session.flush()

    timeline.add_event(
        subject_type="PRODUCTION_ORDER",
        subject_id=production_order.id,
        event_type="PLAN_CHANGED",
        old_value="2026-01-10",
        new_value="2026-01-12",
        reason_code="PRODUCTION_DELAY",
        event_at=datetime(2026, 1, 1, tzinfo=UTC),
        source="SAP",
    )
    timeline.add_event(
        subject_type="PRODUCTION_ORDER",
        subject_id=production_order.id,
        event_type="COMPLETED",
        reason_code="DONE",
        event_at=datetime(2026, 1, 2, tzinfo=UTC),
        source="SAP",
    )

    events = timeline.list_events_for_subject("PRODUCTION_ORDER", production_order.id)
    assert [e["event_type"] for e in events] == ["PLAN_CHANGED", "COMPLETED"]

    latest = timeline.latest_event_for_subject("PRODUCTION_ORDER", production_order.id)
    assert latest is not None
    assert latest["event_type"] == "COMPLETED"

    assert timeline.latest_event_for_subject("QA_LOT", production_order.id) is None


def test_list_open_production_orders_excludes_complete(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    material = repos.master_data.add_material("MAT-1")
    plant = repos.master_data.add_plant("PLANT-1")
    db_session.add_all(
        [
            ProductionOrder(
                production_order_number="PO-OPEN-1",
                material_id=material["id"],
                plant_id=plant["id"],
                status="IN_PROGRESS",
                planned_end_date=date(2026, 2, 1),
            ),
            ProductionOrder(
                production_order_number="PO-DONE-1",
                material_id=material["id"],
                plant_id=plant["id"],
                status="COMPLETE",
                planned_end_date=date(2026, 1, 15),
            ),
        ]
    )
    db_session.flush()

    open_orders = timeline.list_open_production_orders(material["id"], plant["id"])

    assert [o["production_order_number"] for o in open_orders] == ["PO-OPEN-1"]


def test_list_open_quality_lots_filters_in_inspection(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    material = repos.master_data.add_material("MAT-QA")
    plant = repos.master_data.add_plant("PLANT-QA")
    db_session.add_all(
        [
            QualityLot(
                lot_number="LOT-OPEN",
                material_id=material["id"],
                plant_id=plant["id"],
                quantity=50,
                inspection_start_date=date(2026, 1, 1),
                planned_release_date=date(2026, 1, 5),
                status="IN_INSPECTION",
            ),
            QualityLot(
                lot_number="LOT-RELEASED",
                material_id=material["id"],
                plant_id=plant["id"],
                quantity=50,
                inspection_start_date=date(2026, 1, 1),
                planned_release_date=date(2026, 1, 3),
                status="RELEASED",
            ),
        ]
    )
    db_session.flush()

    open_lots = timeline.list_open_quality_lots(material["id"], plant["id"])

    assert [lot["lot_number"] for lot in open_lots] == ["LOT-OPEN"]


def test_get_on_hand_defaults_to_zero_when_unset_or_missing(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    material = repos.master_data.add_material("MAT-STOCK")
    plant = repos.master_data.add_plant("PLANT-STOCK")

    assert timeline.get_on_hand(material["id"], plant["id"]) == 0.0

    db_session.add(
        MaterialMaster(material_id=material["id"], plant_id=plant["id"], sap_material_number="SAP-1")
    )
    db_session.flush()
    assert timeline.get_on_hand(material["id"], plant["id"]) == 0.0

    master = timeline.get_material_master(material["id"], plant["id"])
    master_row = db_session.get(MaterialMaster, master["id"])
    master_row.available_quantity = 42.5
    db_session.flush()

    assert timeline.get_on_hand(material["id"], plant["id"]) == 42.5


def test_get_warehouse_plant_id(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    plant = repos.master_data.add_plant("PLANT-WH")
    warehouse = repos.master_data.add_warehouse("WH-1", plant_id=plant["id"])

    assert timeline.get_warehouse_plant_id(warehouse["id"]) == plant["id"]
    assert timeline.get_warehouse_plant_id(plant["id"]) is None


def test_list_open_plans_and_list_plans_for_purchase_order(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    open_plan = _seed_plan(repos, timeline, "PLAN-OPEN")
    shipped_plan = timeline.create_plan(
        plan_number="PLAN-SHIPPED", purchase_order_id=open_plan["purchase_order_id"], freight_term="PREPAID"
    )
    db_session.execute(
        update(FulfillmentPlan).where(FulfillmentPlan.id == shipped_plan["id"]).values(status="SHIPPED")
    )
    cancelled = _seed_plan(repos, timeline, "PLAN-CANCELLED")
    db_session.execute(
        update(FulfillmentPlan).where(FulfillmentPlan.id == cancelled["id"]).values(status="CANCELLED")
    )
    db_session.flush()

    open_plans = {p["id"] for p in timeline.list_open_plans()}
    assert open_plan["id"] in open_plans
    assert shipped_plan["id"] in open_plans
    assert cancelled["id"] not in open_plans

    for_po = timeline.list_plans_for_purchase_order(open_plan["purchase_order_id"])
    assert {p["id"] for p in for_po} == {open_plan["id"], shipped_plan["id"]}
