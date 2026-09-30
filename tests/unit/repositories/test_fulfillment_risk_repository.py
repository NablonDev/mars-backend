"""Tests for `FulfillmentRiskRepository`."""

from __future__ import annotations

from datetime import date

from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.penalties.fulfillment_risk import FulfillmentRiskRepository


def _seed_plan(repos, timeline: FulfillmentTimelineRepository, retailer_code: str, plan_number: str) -> dict:
    retailer = repos.master_data.add_retailer(retailer_code, "Retailer", None, "SUM")
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=f"PO-{plan_number}", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    plan = timeline.create_plan(
        plan_number=plan_number, purchase_order_id=purchase_order["id"], freight_term="PREPAID"
    )
    return {**plan, "retailer_id": retailer["id"]}


def _risk(**overrides) -> dict:
    base = {
        "purchase_order_id": None,
        "risk_type": "LATE",
        "status": "PROJECTED_BREACH",
        "measured_milestone_code": "DELIVERED",
        "projected_measured_date": date(2026, 1, 10),
        "window_start": date(2026, 1, 1),
        "window_end": date(2026, 1, 8),
        "days_off": 2,
        "shortfall_quantity": None,
        "driver_milestone_code": "PICKED",
        "driver_reason_code": "CARRIER_DELAY",
        "driver_event_id": None,
        "projected_penalty_amount": 100.0,
        "currency_code": "USD",
        "priced_rule_ids": ["RULE-1"],
        "projected_milestones": [{"code": "DELIVERED", "slip_days": 2}],
    }
    base.update(overrides)
    return base


def _option(**overrides) -> dict:
    base = {
        "purchase_order_id": None,
        "action_code": "ACCEPT",
        "owner_team": "Customer Service",
        "feasible": True,
        "infeasible_reason": None,
        "act_by_date": None,
        "penalty_before": 100.0,
        "penalty_after": 100.0,
        "action_cost": 0.0,
        "net_saving": 0.0,
        "confidence": "CONFIRMED",
        "rank_no": 1,
        "addresses_risk_types": ["LATE"],
        "rationale": "Accepts the projected penalty with no mitigation.",
    }
    base.update(overrides)
    return base


def test_replace_for_plan_date_is_idempotent_on_rerun(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    risks_repo = FulfillmentRiskRepository(db_session)
    plan = _seed_plan(repos, timeline, "RET-RISK-1", "PLAN-RISK-1")

    risks_repo.replace_for_plan_date(
        plan["id"],
        date(2026, 1, 5),
        [_risk(purchase_order_id=plan["purchase_order_id"])],
        [_option(purchase_order_id=plan["purchase_order_id"])],
    )
    risks_repo.replace_for_plan_date(
        plan["id"],
        date(2026, 1, 5),
        [_risk(purchase_order_id=plan["purchase_order_id"], projected_penalty_amount=250.0)],
        [_option(purchase_order_id=plan["purchase_order_id"], action_cost=50.0)],
    )

    risk_rows = risks_repo.list_for_plan(plan["id"], date(2026, 1, 5))
    option_rows = risks_repo.list_options_for_plan(plan["id"], date(2026, 1, 5))

    assert len(risk_rows) == 1
    assert risk_rows[0]["projected_penalty_amount"] == 250.0
    assert len(option_rows) == 1
    assert option_rows[0]["action_cost"] == 50.0


def test_list_for_plan_returns_full_history_without_a_date(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    risks_repo = FulfillmentRiskRepository(db_session)
    plan = _seed_plan(repos, timeline, "RET-RISK-2", "PLAN-RISK-2")

    risks_repo.replace_for_plan_date(
        plan["id"], date(2026, 1, 1), [_risk(purchase_order_id=plan["purchase_order_id"])], []
    )
    risks_repo.replace_for_plan_date(
        plan["id"], date(2026, 1, 2), [_risk(purchase_order_id=plan["purchase_order_id"])], []
    )

    history = risks_repo.list_for_plan(plan["id"])

    assert [r["projection_date"] for r in history] == [date(2026, 1, 1), date(2026, 1, 2)]


def test_list_risks_without_date_returns_latest_per_plan(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    risks_repo = FulfillmentRiskRepository(db_session)
    plan_a = _seed_plan(repos, timeline, "RET-RISK-3A", "PLAN-RISK-3A")
    plan_b = _seed_plan(repos, timeline, "RET-RISK-3B", "PLAN-RISK-3B")

    risks_repo.replace_for_plan_date(
        plan_a["id"],
        date(2026, 1, 1),
        [_risk(purchase_order_id=plan_a["purchase_order_id"], risk_type="LATE")],
        [],
    )
    risks_repo.replace_for_plan_date(
        plan_a["id"],
        date(2026, 1, 2),
        [_risk(purchase_order_id=plan_a["purchase_order_id"], risk_type="LATE")],
        [],
    )
    risks_repo.replace_for_plan_date(
        plan_b["id"],
        date(2026, 1, 1),
        [_risk(purchase_order_id=plan_b["purchase_order_id"], risk_type="SHORT")],
        [],
    )

    latest = risks_repo.list_risks()

    by_plan = {r["fulfillment_plan_id"]: r for r in latest}
    assert by_plan[plan_a["id"]]["projection_date"] == date(2026, 1, 2)
    assert by_plan[plan_b["id"]]["projection_date"] == date(2026, 1, 1)


def test_list_risks_filters_by_retailer(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    risks_repo = FulfillmentRiskRepository(db_session)
    plan_a = _seed_plan(repos, timeline, "RET-RISK-4A", "PLAN-RISK-4A")
    plan_b = _seed_plan(repos, timeline, "RET-RISK-4B", "PLAN-RISK-4B")

    risks_repo.replace_for_plan_date(
        plan_a["id"], date(2026, 1, 1), [_risk(purchase_order_id=plan_a["purchase_order_id"])], []
    )
    risks_repo.replace_for_plan_date(
        plan_b["id"], date(2026, 1, 1), [_risk(purchase_order_id=plan_b["purchase_order_id"])], []
    )

    filtered = risks_repo.list_risks(retailer_id=plan_a["retailer_id"])

    assert {r["fulfillment_plan_id"] for r in filtered} == {plan_a["id"]}


def test_list_latest_for_plan_returns_only_the_most_recent_projection_date(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    risks_repo = FulfillmentRiskRepository(db_session)
    plan = _seed_plan(repos, timeline, "RET-RISK-6", "PLAN-RISK-6")

    risks_repo.replace_for_plan_date(
        plan["id"],
        date(2026, 1, 1),
        [_risk(purchase_order_id=plan["purchase_order_id"], risk_type="LATE")],
        [],
    )
    risks_repo.replace_for_plan_date(
        plan["id"],
        date(2026, 1, 2),
        [
            _risk(purchase_order_id=plan["purchase_order_id"], risk_type="LATE"),
            _risk(purchase_order_id=plan["purchase_order_id"], risk_type="SHORT"),
        ],
        [],
    )

    latest = risks_repo.list_latest_for_plan(plan["id"])

    assert {r["projection_date"] for r in latest} == {date(2026, 1, 2)}
    assert {r["risk_type"] for r in latest} == {"LATE", "SHORT"}


def test_list_latest_for_plan_returns_empty_when_never_run(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    risks_repo = FulfillmentRiskRepository(db_session)
    plan = _seed_plan(repos, timeline, "RET-RISK-7", "PLAN-RISK-7")

    assert risks_repo.list_latest_for_plan(plan["id"]) == []


def test_list_risks_with_date_and_status_filters(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    risks_repo = FulfillmentRiskRepository(db_session)
    plan = _seed_plan(repos, timeline, "RET-RISK-5", "PLAN-RISK-5")

    risks_repo.replace_for_plan_date(
        plan["id"],
        date(2026, 1, 1),
        [
            _risk(purchase_order_id=plan["purchase_order_id"], risk_type="LATE", status="PROJECTED_BREACH"),
            _risk(purchase_order_id=plan["purchase_order_id"], risk_type="SHORT", status="SLIPPING"),
        ],
        [],
    )

    breached_only = risks_repo.list_risks(projection_date=date(2026, 1, 1), status="PROJECTED_BREACH")

    assert [r["risk_type"] for r in breached_only] == ["LATE"]
