"""Tests for app/workers/penalty_timeline.py -- the nightly event-driven
timeline projection step that runs alongside the legacy probability engine
(see docs/architecture/penalty-timeline-engine.md).

Mirrors `app/workers/penalty_projection.py`'s pattern: build the service
from repositories inside `database.session()`, then delegate to the
service's own entry point (`TimelineProjectionService.run_for_all_open`,
already covered end to end by
`tests/unit/services/penalties/timeline/test_service.py`). These tests
exercise `run_daily_timeline`'s own wiring/session lifecycle, not the
projection math itself.

Run against the same in-memory SQLite `database`/`db_session` fixtures as
the rest of the worker tests (see tests/conftest.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.db.session import Database
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.workers.penalty_timeline import run_daily_timeline
from scripts.seed.seed_milestone_types import seed_milestone_types

_TODAY = date(2026, 8, 14)


@dataclass
class _Scenario:
    plan_id: object
    purchase_order_id: object


def _build_open_plan(db_session, *, window_start: date, window_end: date) -> _Scenario:
    master_data = MasterDataRepository(db_session)
    purchase_orders = PurchaseOrderRepository(db_session)
    timeline = FulfillmentTimelineRepository(db_session)

    retailer = master_data.add_retailer("RET-TL", "Retailer TL", None, "SUM")
    plant = master_data.add_plant("PLANT-TL")
    material = master_data.add_material("MAT-TL")
    purchase_order = purchase_orders.create_purchase_order(
        purchase_order_number="PO-TL",
        retailer_id=retailer["id"],
        order_date=date(2026, 1, 1),
        window_start=window_start,
        window_end=window_end,
        cancel_date=None,
        freight_term="PREPAID",
    )
    po_line = purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100.0,
        unit_price=20.0,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    plan = timeline.create_plan(
        plan_number="PLAN-TL",
        purchase_order_id=purchase_order["id"],
        freight_term="PREPAID",
        planned_transit_days=2,
    )
    timeline.add_plan_line(plan["id"], po_line["id"], planned_quantity=100.0)
    db_session.commit()
    return _Scenario(plan_id=plan["id"], purchase_order_id=purchase_order["id"])


def test_run_daily_timeline_with_no_open_plans_returns_an_empty_summary(database: Database, db_session):
    seed_milestone_types(db_session)
    db_session.commit()

    summary = run_daily_timeline(database, _TODAY)

    assert summary.plans_evaluated == 0
    assert summary.plans_skipped == 0
    assert summary.status_counts == {}
    assert summary.total_projected_penalty == 0.0


def test_run_daily_timeline_evaluates_an_open_plan_end_to_end(database: Database, db_session):
    seed_milestone_types(db_session)
    scenario = _build_open_plan(db_session, window_start=date(2026, 8, 20), window_end=date(2026, 8, 25))

    summary = run_daily_timeline(database, _TODAY)

    assert summary.plans_evaluated == 1
    assert summary.plans_skipped == 0
    assert sum(summary.status_counts.values()) == 1

    with database.session() as session:
        timeline = FulfillmentTimelineRepository(session)
        plan = timeline.get_plan(scenario.plan_id)
    assert plan is not None


def test_run_daily_timeline_runs_the_given_projection_date(database: Database, db_session, monkeypatch):
    """`run_daily_timeline` takes the already-resolved business-timezone date as a
    required argument (mirrors `enqueue_daily_run`'s resolved date), rather than
    resolving "today" itself -- the script resolves it once and passes the same
    value to both the legacy enqueue and this step."""
    seed_milestone_types(db_session)
    db_session.commit()

    called_with: list[date] = []

    from app.services.penalties.timeline import service as service_module

    original = service_module.TimelineProjectionService.run_for_all_open

    def _spy(self, projection_date=None):
        called_with.append(projection_date)
        return original(self, projection_date)

    monkeypatch.setattr(service_module.TimelineProjectionService, "run_for_all_open", _spy)

    run_daily_timeline(database, _TODAY)

    assert called_with == [_TODAY]
