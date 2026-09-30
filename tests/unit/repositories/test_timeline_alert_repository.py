"""Tests for `TimelineAlertRepository`."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from app.core.exceptions import NotFoundError
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.penalties.timeline_alert import TimelineAlertRepository
from app.services.penalties.timeline.alerts import AlertChange

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def _seed_plan(repos, timeline: FulfillmentTimelineRepository, retailer_code: str, plan_number: str) -> dict:
    retailer = repos.master_data.add_retailer(retailer_code, "Retailer", None, "SUM")
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=f"PO-{plan_number}", retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    plan = timeline.create_plan(
        plan_number=plan_number, purchase_order_id=purchase_order["id"], freight_term="PREPAID"
    )
    return {**plan, "purchase_order_id": purchase_order["id"]}


def _create_change(**field_overrides) -> AlertChange:
    fields = {
        "status": "NEW",
        "change_type": "NEW",
        "first_seen_date": date(2026, 1, 10),
        "last_seen_date": date(2026, 1, 10),
        "prev_penalty_amount": None,
        "last_penalty_amount": 100.0,
        "prev_days_off": None,
        "last_days_off": 2,
        "prev_shortfall_quantity": None,
        "last_shortfall_quantity": None,
        "is_tracking": True,
    }
    fields.update(field_overrides)
    return AlertChange(kind="CREATE", risk_type="LATE", alert_id=None, fields=fields)


def test_apply_changes_creates_a_new_alert(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    alerts = TimelineAlertRepository(db_session)
    plan = _seed_plan(repos, timeline, "RET-TA1", "PLAN-TA1")

    alerts.apply_changes(plan["id"], plan["purchase_order_id"], [_create_change()])

    rows = alerts.list_for_plan(plan["id"])
    assert len(rows) == 1
    assert rows[0]["risk_type"] == "LATE"
    assert rows[0]["status"] == "NEW"
    assert rows[0]["is_tracking"] is True


def test_list_tracking_for_plan_returns_only_tracking_alerts(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    alerts = TimelineAlertRepository(db_session)
    plan = _seed_plan(repos, timeline, "RET-TA2", "PLAN-TA2")
    alerts.apply_changes(plan["id"], plan["purchase_order_id"], [_create_change()])
    created = alerts.list_for_plan(plan["id"])[0]

    stop_tracking = AlertChange(
        kind="STOP_TRACKING", risk_type="LATE", alert_id=created["alert_id"], fields={"is_tracking": False}
    )
    alerts.apply_changes(plan["id"], plan["purchase_order_id"], [stop_tracking])

    assert alerts.list_tracking_for_plan(plan["id"]) == []
    assert alerts.list_for_plan(plan["id"])[0]["is_tracking"] is False


def test_apply_changes_updates_an_existing_alert(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    alerts = TimelineAlertRepository(db_session)
    plan = _seed_plan(repos, timeline, "RET-TA3", "PLAN-TA3")
    alerts.apply_changes(plan["id"], plan["purchase_order_id"], [_create_change()])
    created = alerts.list_for_plan(plan["id"])[0]

    update = AlertChange(
        kind="UPDATE",
        risk_type="LATE",
        alert_id=created["alert_id"],
        fields={
            "last_seen_date": date(2026, 1, 11),
            "prev_penalty_amount": 100.0,
            "last_penalty_amount": 150.0,
            "prev_days_off": 2,
            "last_days_off": 3,
            "prev_shortfall_quantity": None,
            "last_shortfall_quantity": None,
            "change_type": "WORSE",
        },
    )
    alerts.apply_changes(plan["id"], plan["purchase_order_id"], [update])

    tracking = alerts.list_tracking_for_plan(plan["id"])
    assert len(tracking) == 1
    assert tracking[0]["last_penalty_amount"] == 150.0
    assert tracking[0]["prev_penalty_amount"] == 100.0
    assert tracking[0]["alert_id"] == created["alert_id"]


def test_apply_changes_closes_an_alert(repos, db_session):
    timeline = FulfillmentTimelineRepository(db_session)
    alerts = TimelineAlertRepository(db_session)
    plan = _seed_plan(repos, timeline, "RET-TA4", "PLAN-TA4")
    alerts.apply_changes(plan["id"], plan["purchase_order_id"], [_create_change()])
    created = alerts.list_for_plan(plan["id"])[0]

    close = AlertChange(
        kind="CLOSE",
        risk_type="LATE",
        alert_id=created["alert_id"],
        fields={
            "status": "RESOLVED",
            "closed_reason": "AUTO_CLEARED",
            "closed_at": NOW,
            "is_tracking": False,
        },
    )
    alerts.apply_changes(plan["id"], plan["purchase_order_id"], [close])

    row = alerts.get(created["alert_id"])
    assert row["status"] == "RESOLVED"
    assert row["closed_reason"] == "AUTO_CLEARED"
    assert row["is_tracking"] is False


def test_get_raises_not_found_for_unknown_alert(db_session):
    alerts = TimelineAlertRepository(db_session)

    try:
        alerts.get(uuid4())
        raise AssertionError("expected NotFoundError")
    except NotFoundError:
        pass
