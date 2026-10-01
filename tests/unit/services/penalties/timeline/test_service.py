"""End-to-end tests for `TimelineProjectionService`, against SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.exceptions import NotFoundError
from app.models import ProductionOrder, QualityLot
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.penalties.fulfillment_risk import FulfillmentRiskRepository
from app.repositories.penalties.timeline_alert import TimelineAlertRepository
from app.services.penalties.timeline.schedule import backward_schedule
from app.services.penalties.timeline.service import TimelineProjectionService
from scripts.seed.seed_milestone_types import seed_milestone_types
from tests.conftest import make_retailer_agreement


@dataclass
class _Scenario:
    plan_id: object
    purchase_order_id: object
    material_id: object
    plant_id: object


@pytest.fixture
def timeline_repos(repos, db_session):
    seed_milestone_types(db_session)
    return {
        "timeline": FulfillmentTimelineRepository(db_session),
        "risks": FulfillmentRiskRepository(db_session),
        "alerts": TimelineAlertRepository(db_session),
    }


@pytest.fixture
def service(repos, timeline_repos) -> TimelineProjectionService:
    return TimelineProjectionService(
        timeline=timeline_repos["timeline"],
        risks=timeline_repos["risks"],
        alerts=timeline_repos["alerts"],
        purchase_orders=repos.purchase_orders,
        rules=repos.penalty_rules,
        master_data=repos.master_data,
    )


def _build_plan(
    repos,
    timeline: FulfillmentTimelineRepository,
    *,
    tag: str,
    window_start: date,
    window_end: date,
    planned_quantity: float = 100.0,
) -> _Scenario:
    retailer = repos.master_data.add_retailer(f"RET-{tag}", f"Retailer {tag}", None, "SUM")
    plant = repos.master_data.add_plant(f"PLANT-{tag}")
    material = repos.master_data.add_material(f"MAT-{tag}")
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=f"PO-{tag}",
        retailer_id=retailer["id"],
        order_date=date(2026, 1, 1),
        window_start=window_start,
        window_end=window_end,
        cancel_date=None,
        freight_term="PREPAID",
    )
    po_line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=planned_quantity,
        unit_price=20.0,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    plan = timeline.create_plan(
        plan_number=f"PLAN-{tag}",
        purchase_order_id=purchase_order["id"],
        freight_term="PREPAID",
        planned_transit_days=2,
    )
    timeline.add_plan_line(plan["id"], po_line["id"], planned_quantity=planned_quantity)
    return _Scenario(
        plan_id=plan["id"],
        purchase_order_id=purchase_order["id"],
        material_id=material["id"],
        plant_id=plant["id"],
    )


def _retailer_id(repos, purchase_order_id) -> object:
    return repos.purchase_orders.get_purchase_order(purchase_order_id)["retailer_id"]


def test_late_projected_breach_from_a_picked_slip_is_priced_and_gets_ranked_options(
    repos, timeline_repos, service
):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="LATE",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )
    agreement_id = make_retailer_agreement(repos, _retailer_id(repos, scenario.purchase_order_id))
    repos.penalty_rules.add_rule(
        rule_code="RULE-OTIF-LATE",
        violation_type="OTIF_LATE",
        penalty_category="LATE",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=2.0,
    )
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )

    outcome = service.run_for_plan(scenario.plan_id, projection_date=date(2026, 1, 3))

    assert outcome.skipped is False
    assert outcome.status == "PROJECTED_BREACH"
    assert outcome.total_projected_penalty > 0

    risk_rows = timeline_repos["risks"].list_for_plan(scenario.plan_id, date(2026, 1, 3))
    assert len(risk_rows) == 1
    late_row = risk_rows[0]
    assert late_row["risk_type"] == "LATE"
    assert late_row["status"] == "PROJECTED_BREACH"
    assert late_row["driver_milestone_code"] == "PICKED"
    assert late_row["projected_penalty_amount"] > 0

    detail = late_row["calculation_detail"]
    assert detail["measured"]["milestone_code"] == late_row["measured_milestone_code"]
    assert detail["measured"]["window_start"] == "2026-01-01"
    assert detail["measured"]["window_end"] == "2026-01-06"
    assert detail["pricing"]["stacking_mode"] == "SUM"
    assert detail["pricing"]["quantity"] == 100.0
    rule_detail = detail["pricing"]["rules"][0]
    assert rule_detail["rule_code"] == "RULE-OTIF-LATE"
    assert rule_detail["violation_type"] == "OTIF_LATE"
    assert rule_detail["amount"] == late_row["projected_penalty_amount"]
    assert detail["supply"] is None

    options = timeline_repos["risks"].list_options_for_plan(scenario.plan_id, date(2026, 1, 3))
    assert options
    feasible_ranked = [o for o in options if o["feasible"]]
    assert feasible_ranked
    assert feasible_ranked[0]["rank_no"] == 1
    assert any(o["action_code"] == "ACCEPT" for o in options)


def test_driver_reason_survives_the_driver_milestones_own_completion(repos, timeline_repos, service):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="REPLAN-THEN-DONE",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )
    agreement_id = make_retailer_agreement(repos, _retailer_id(repos, scenario.purchase_order_id))
    repos.penalty_rules.add_rule(
        rule_code="RULE-OTIF-LATE-REPLAN-DONE",
        violation_type="OTIF_LATE",
        penalty_category="LATE",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=2.0,
    )
    milestone = timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )
    timeline_repos["timeline"].add_event(
        subject_type="PLAN_MILESTONE",
        subject_id=milestone["id"],
        fulfillment_plan_id=scenario.plan_id,
        milestone_type_id=milestone["milestone_type_id"],
        event_type="PLAN_CHANGED",
        old_value="2026-01-03",
        new_value="2026-01-10",
        reason_code="CARRIER_DELAY",
        event_at=datetime(2026, 1, 2, tzinfo=UTC),
        source="SIMULATOR",
    )

    # PICKED actually completes on its replanned date; its completion event
    # carries no reason of its own (a plain fact, not a cause).
    timeline_repos["timeline"].upsert_milestone(scenario.plan_id, "PICKED", actual_date=date(2026, 1, 10))
    timeline_repos["timeline"].add_event(
        subject_type="PLAN_MILESTONE",
        subject_id=milestone["id"],
        fulfillment_plan_id=scenario.plan_id,
        milestone_type_id=milestone["milestone_type_id"],
        event_type="COMPLETED",
        old_value=None,
        new_value="2026-01-10",
        reason_code=None,
        event_at=datetime(2026, 1, 10, tzinfo=UTC),
        source="SIMULATOR",
    )

    service.run_for_plan(scenario.plan_id, projection_date=date(2026, 1, 10))

    risk_rows = timeline_repos["risks"].list_for_plan(scenario.plan_id, date(2026, 1, 10))
    late_row = next(r for r in risk_rows if r["risk_type"] == "LATE")
    assert late_row["status"] == "PROJECTED_BREACH"
    assert late_row["driver_milestone_code"] == "PICKED"
    assert late_row["driver_reason_code"] == "CARRIER_DELAY"


def test_qa_lot_delay_produces_a_short_risk_with_qa_hold_cause(repos, timeline_repos, service, db_session):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="SHORT",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 2, 1),
        planned_quantity=100.0,
    )
    agreement_id = make_retailer_agreement(
        repos, repos.purchase_orders.get_purchase_order(scenario.purchase_order_id)["retailer_id"]
    )
    repos.penalty_rules.add_rule(
        rule_code="RULE-SHORT-SHIP",
        violation_type="SHORT_SHIP",
        penalty_category="SHORT",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=3.0,
    )
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "MATERIAL_AVAILABLE", baseline_date=date(2026, 1, 5), planned_date=date(2026, 1, 5)
    )

    quality_lot = QualityLot(
        lot_number="LOT-QA-SHORT",
        material_id=scenario.material_id,
        plant_id=scenario.plant_id,
        quantity=40.0,
        inspection_start_date=date(2026, 1, 1),
        planned_release_date=date(2026, 1, 10),
        status="IN_INSPECTION",
    )
    db_session.add(quality_lot)
    db_session.flush()

    timeline_repos["timeline"].add_event(
        subject_type="QA_LOT",
        subject_id=quality_lot.id,
        event_type="PLAN_CHANGED",
        old_value="2026-01-05",
        new_value="2026-01-10",
        reason_code="QA_HOLD",
        event_at=datetime(2026, 1, 2, tzinfo=UTC),
        source="SAP",
    )

    outcome = service.run_for_plan(scenario.plan_id, projection_date=date(2026, 1, 3))

    assert outcome.status == "PROJECTED_BREACH"
    risk_rows = timeline_repos["risks"].list_for_plan(scenario.plan_id, date(2026, 1, 3))
    short_rows = [r for r in risk_rows if r["risk_type"] == "SHORT"]
    assert len(short_rows) == 1
    assert short_rows[0]["status"] == "PROJECTED_BREACH"
    assert short_rows[0]["shortfall_quantity"] == pytest.approx(60.0)
    assert short_rows[0]["driver_reason_code"] == "QA_HOLD"
    assert short_rows[0]["projected_penalty_amount"] > 0

    detail = short_rows[0]["calculation_detail"]
    assert detail["pricing"]["rules"][0]["rule_code"] == "RULE-SHORT-SHIP"
    assert detail["pricing"]["rules"][0]["shortfall_quantity"] == pytest.approx(60.0)
    assert detail["supply"] is not None
    assert detail["supply"]["shortfall_quantity"] == pytest.approx(60.0)
    assert detail["supply"]["cause_code"] == "QA_HOLD"
    assert detail["supply"]["cause_source_type"] == "QA_LOT"


def test_mitigation_driver_follows_the_costliest_breach_not_insertion_order(
    repos, timeline_repos, service, db_session
):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="MULTI-DRIVER",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
        planned_quantity=100.0,
    )
    agreement_id = make_retailer_agreement(repos, _retailer_id(repos, scenario.purchase_order_id))
    repos.penalty_rules.add_rule(
        rule_code="RULE-OTIF-LATE-MULTI",
        violation_type="OTIF_LATE",
        penalty_category="LATE",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=2.0,
    )
    repos.penalty_rules.add_rule(
        rule_code="RULE-SHORT-SHIP-MULTI",
        violation_type="SHORT_SHIP",
        penalty_category="SHORT",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=5.0,
    )

    # PICKED-driven LATE: a modest, flat PER_UNIT-priced delay (100 * 2.0 = 200).
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )

    # QA_HOLD-driven SHORT: a 60-unit shortfall priced higher (60 * 5.0 = 300).
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "MATERIAL_AVAILABLE", baseline_date=date(2026, 1, 5), planned_date=date(2026, 1, 5)
    )
    quality_lot = QualityLot(
        lot_number="LOT-QA-MULTI",
        material_id=scenario.material_id,
        plant_id=scenario.plant_id,
        quantity=40.0,
        inspection_start_date=date(2026, 1, 1),
        planned_release_date=date(2026, 1, 10),
        status="IN_INSPECTION",
    )
    db_session.add(quality_lot)
    db_session.flush()
    timeline_repos["timeline"].add_event(
        subject_type="QA_LOT",
        subject_id=quality_lot.id,
        event_type="PLAN_CHANGED",
        old_value="2026-01-05",
        new_value="2026-01-10",
        reason_code="QA_HOLD",
        event_at=datetime(2026, 1, 2, tzinfo=UTC),
        source="SAP",
    )

    outcome = service.run_for_plan(scenario.plan_id, projection_date=date(2026, 1, 3))

    assert outcome.status == "PROJECTED_BREACH"
    risk_rows = timeline_repos["risks"].list_for_plan(scenario.plan_id, date(2026, 1, 3))
    late_row = next(r for r in risk_rows if r["risk_type"] == "LATE")
    short_row = next(r for r in risk_rows if r["risk_type"] == "SHORT")
    assert short_row["projected_penalty_amount"] > late_row["projected_penalty_amount"]

    options = {
        o["action_code"]: o
        for o in timeline_repos["risks"].list_options_for_plan(scenario.plan_id, date(2026, 1, 3))
    }
    # The costliest breach (SHORT, driven by QA_HOLD) is the root cause mitigation
    # matches against, not LATE (which was inserted first).
    assert options["EXPEDITE_QA_RELEASE"]["feasible"] is True
    assert options["PRIORITIZE_PICK"]["feasible"] is False
    assert options["PRIORITIZE_PICK"]["infeasible_reason"] == "ROOT_CAUSE_MISMATCH"


def test_wait_policy_production_delay_reports_production_delay_on_the_late_row(
    repos, timeline_repos, service, db_session
):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="WAIT",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 10),
        planned_quantity=100.0,
    )
    agreement_id = make_retailer_agreement(repos, _retailer_id(repos, scenario.purchase_order_id))
    repos.penalty_rules.add_rule(
        rule_code="RULE-OTIF-LATE-WAIT",
        violation_type="OTIF_LATE",
        penalty_category="LATE",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=2.0,
    )
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "MATERIAL_AVAILABLE", baseline_date=date(2026, 1, 5), planned_date=date(2026, 1, 5)
    )

    production_order = ProductionOrder(
        production_order_number="PO-ORDER-WAIT",
        material_id=scenario.material_id,
        plant_id=scenario.plant_id,
        planned_quantity=150.0,
        planned_end_date=date(2026, 1, 9),
        status="IN_PROGRESS",
    )
    db_session.add(production_order)
    db_session.flush()

    timeline_repos["timeline"].add_event(
        subject_type="PRODUCTION_ORDER",
        subject_id=production_order.id,
        event_type="PLAN_CHANGED",
        old_value="2026-01-05",
        new_value="2026-01-09",
        reason_code="PRODUCTION_DELAY",
        event_at=datetime(2026, 1, 2, tzinfo=UTC),
        source="SAP",
    )

    outcome = service.run_for_plan(scenario.plan_id, projection_date=date(2026, 1, 3))

    assert outcome.status == "PROJECTED_BREACH"
    risk_rows = timeline_repos["risks"].list_for_plan(scenario.plan_id, date(2026, 1, 3))
    late_rows = [r for r in risk_rows if r["risk_type"] == "LATE"]
    assert len(late_rows) == 1
    assert late_rows[0]["driver_milestone_code"] == "MATERIAL_AVAILABLE"
    assert late_rows[0]["driver_reason_code"] == "PRODUCTION_DELAY"
    # WAIT-policy: fully covered (late), so no SHORT row alongside the LATE one.
    assert not any(r["risk_type"] == "SHORT" for r in risk_rows)


def test_slip_absorbed_by_slack_writes_exactly_one_slipping_row_with_no_options(
    repos, timeline_repos, service
):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="SLACK",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 30),
    )
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 5)
    )

    outcome = service.run_for_plan(scenario.plan_id, projection_date=date(2026, 1, 3))

    assert outcome.status == "SLIPPING"
    risk_rows = timeline_repos["risks"].list_for_plan(scenario.plan_id, date(2026, 1, 3))
    assert len(risk_rows) == 1
    assert risk_rows[0]["risk_type"] == "LATE"
    assert risk_rows[0]["status"] == "SLIPPING"
    assert risk_rows[0]["projected_penalty_amount"] == 0.0

    options = timeline_repos["risks"].list_options_for_plan(scenario.plan_id, date(2026, 1, 3))
    assert options == []


def test_run_for_all_open_commits_each_plan_so_an_earlier_plans_rows_survive_a_later_failure(
    repos, timeline_repos, service, monkeypatch
):
    surviving = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="SURVIVE",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )
    agreement_id = make_retailer_agreement(repos, _retailer_id(repos, surviving.purchase_order_id))
    repos.penalty_rules.add_rule(
        rule_code="RULE-OTIF-LATE-SURVIVE",
        violation_type="OTIF_LATE",
        penalty_category="LATE",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=2.0,
    )
    timeline_repos["timeline"].upsert_milestone(
        surviving.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )
    failing = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="FAILING",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 3, 1),
    )

    # `list_open_plans` orders oldest-created first, so `surviving` (created
    # first) is processed before `failing` -- see the repository docstring.
    ordered_ids = [p["id"] for p in timeline_repos["timeline"].list_open_plans()]
    assert ordered_ids == [surviving.plan_id, failing.plan_id]

    original_build_risk_rows = type(service._rows).build_risk_rows

    def failing_build_risk_rows(self, plan_id, *args, **kwargs):
        if plan_id == failing.plan_id:
            raise RuntimeError("boom-on-second-plan")
        return original_build_risk_rows(self, plan_id, *args, **kwargs)

    monkeypatch.setattr(type(service._rows), "build_risk_rows", failing_build_risk_rows)

    with pytest.raises(RuntimeError, match="boom-on-second-plan"):
        service.run_for_all_open(projection_date=date(2026, 1, 3))

    survivor_rows = timeline_repos["risks"].list_for_plan(surviving.plan_id, date(2026, 1, 3))
    assert len(survivor_rows) == 1
    assert survivor_rows[0]["risk_type"] == "LATE"


def test_on_track_plan_writes_no_risk_rows(repos, timeline_repos, service):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="TRACK",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 3, 1),
    )

    outcome = service.run_for_plan(scenario.plan_id, projection_date=date(2026, 1, 3))

    assert outcome.status == "ON_TRACK"
    assert outcome.total_projected_penalty == 0.0
    assert timeline_repos["risks"].list_for_plan(scenario.plan_id) == []


def test_rerunning_the_same_projection_date_replaces_rows_without_duplicating(repos, timeline_repos, service):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="RERUN",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )
    agreement_id = make_retailer_agreement(repos, _retailer_id(repos, scenario.purchase_order_id))
    repos.penalty_rules.add_rule(
        rule_code="RULE-OTIF-LATE-RERUN",
        violation_type="OTIF_LATE",
        penalty_category="LATE",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=2.0,
    )
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )

    service.run_for_plan(scenario.plan_id, projection_date=date(2026, 1, 3))
    service.run_for_plan(scenario.plan_id, projection_date=date(2026, 1, 3))

    risk_rows = timeline_repos["risks"].list_for_plan(scenario.plan_id, date(2026, 1, 3))
    assert len(risk_rows) == 1


def test_run_for_plan_raises_not_found_for_unknown_plan(service):
    import uuid

    with pytest.raises(NotFoundError) as exc_info:
        service.run_for_plan(uuid.uuid4())
    assert exc_info.value.code == "FULFILLMENT_PLAN_NOT_FOUND"


def test_run_for_all_open_aggregates_across_plans(repos, timeline_repos, service):
    late_scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="ALL-LATE",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )
    agreement_id = make_retailer_agreement(repos, _retailer_id(repos, late_scenario.purchase_order_id))
    repos.penalty_rules.add_rule(
        rule_code="RULE-OTIF-LATE-ALL",
        violation_type="OTIF_LATE",
        penalty_category="LATE",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=2.0,
    )
    timeline_repos["timeline"].upsert_milestone(
        late_scenario.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )
    _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="ALL-TRACK",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 3, 1),
    )

    summary = service.run_for_all_open(projection_date=date(2026, 1, 3))

    assert summary.plans_evaluated == 2
    assert summary.plans_skipped == 0
    assert summary.status_counts.get("PROJECTED_BREACH") == 1
    assert summary.status_counts.get("ON_TRACK") == 1
    assert summary.total_projected_penalty > 0


def test_plan_with_no_po_window_is_skipped(repos, timeline_repos, service):
    retailer = repos.master_data.add_retailer("RET-NOWINDOW", "Retailer", None, "SUM")
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-NOWINDOW", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    plan = timeline_repos["timeline"].create_plan(
        plan_number="PLAN-NOWINDOW", purchase_order_id=purchase_order["id"], freight_term="PREPAID"
    )

    outcome = service.run_for_plan(plan["id"], projection_date=date(2026, 1, 3))

    assert outcome.skipped is True


def test_replayed_plan_gets_one_tracking_alert_that_resolves_when_the_slip_clears(
    repos, timeline_repos, service
):
    window_start, window_end = date(2026, 1, 1), date(2026, 1, 10)
    scenario = _build_plan(
        repos, timeline_repos["timeline"], tag="ALERT-CYCLE", window_start=window_start, window_end=window_end
    )
    agreement_id = make_retailer_agreement(repos, _retailer_id(repos, scenario.purchase_order_id))
    repos.penalty_rules.add_rule(
        rule_code="RULE-OTIF-ALERT-CYCLE",
        violation_type="OTIF_LATE",
        penalty_category="LATE",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=2.0,
    )
    definitions = timeline_repos["timeline"].list_milestone_definitions()
    baseline_dates = backward_schedule(definitions, "PREPAID", date(2026, 1, 1), window_start, window_end, 2)
    for code, milestone_date in baseline_dates.items():
        timeline_repos["timeline"].upsert_milestone(
            scenario.plan_id, code, baseline_date=milestone_date, planned_date=milestone_date
        )
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "PICKED", planned_date=baseline_dates["PICKED"] + timedelta(days=7)
    )

    service.run_for_plan(scenario.plan_id, projection_date=date(2026, 1, 3))
    tracking = timeline_repos["alerts"].list_tracking_for_plan(scenario.plan_id)
    assert len(tracking) == 1
    assert tracking[0]["risk_type"] == "LATE"
    assert tracking[0]["status"] == "NEW"

    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "PICKED", planned_date=baseline_dates["PICKED"]
    )
    service.run_for_plan(scenario.plan_id, projection_date=date(2026, 1, 4))

    assert timeline_repos["alerts"].list_tracking_for_plan(scenario.plan_id) == []
    alerts = timeline_repos["alerts"].list_for_plan(scenario.plan_id)
    late_alert = next(a for a in alerts if a["risk_type"] == "LATE")
    assert late_alert["status"] == "RESOLVED"
    assert late_alert["closed_reason"] == "AUTO_CLEARED"
