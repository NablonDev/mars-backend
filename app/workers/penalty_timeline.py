"""Nightly event-driven fulfillment-timeline projection step.

Runs `TimelineProjectionService.run_for_all_open` alongside the legacy
probability engine (`app/workers/penalty_projection.py`); both engines run
side by side against the same nightly schedule, see
`docs/architecture/penalty-timeline-engine.md`. Mirrors how that module
builds `ProjectionService` from repositories inside `database.session()`.
"""

from __future__ import annotations

from datetime import date

from app.db.session import Database
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.fulfillment_risk import FulfillmentRiskRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.timeline_alert import TimelineAlertRepository
from app.services.penalties.timeline.service import TimelineProjectionService, TimelineRunSummary


def run_daily_timeline(database: Database, projection_date: date) -> TimelineRunSummary:
    """Project every OPEN/SHIPPED fulfillment plan for `projection_date` and summarize.

    `projection_date` is the same business-timezone date already resolved by the
    caller for the legacy engine's `enqueue_daily_run` -- this step does not
    resolve "today" itself.
    """
    with database.session() as session:
        service = TimelineProjectionService(
            timeline=FulfillmentTimelineRepository(session),
            risks=FulfillmentRiskRepository(session),
            alerts=TimelineAlertRepository(session),
            purchase_orders=PurchaseOrderRepository(session),
            rules=PenaltyRuleRepository(session),
            master_data=MasterDataRepository(session),
        )
        return service.run_for_all_open(projection_date)
