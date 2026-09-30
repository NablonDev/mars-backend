"""Replays the fulfillment-timeline scenario catalog day by day against real repositories.

Synthetic, SAP-shaped data: master data, orders, and disruptions are all
`TL-`-prefixed so `reset()` can remove exactly what this seeder owns. Each
simulated day applies that day's disruptions, advances upstream supply
(production orders, QA lots), advances every open plan's milestones, then
runs `TimelineProjectionService.run_for_all_open` so risk history accumulates
across the whole replay, not just at the end.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.fulfillment_risk import FulfillmentRiskRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.timeline_alert import TimelineAlertRepository
from app.services.penalties.timeline.schedule import backward_schedule
from app.services.penalties.timeline.service import TimelineProjectionService
from app.services.penalties.timeline.supply_inputs import SupplyInputBuilder
from app.services.penalties.timeline.types import MilestoneDefinition
from app.services.seeding.timeline.master_data import QA_RELEASE_DAYS, TimelineMasterData
from app.services.seeding.timeline.scenarios import (
    MILESTONE_REPLAN,
    PRODUCTION_REPLAN,
    QA_HOLD,
    SCENARIOS,
    TRANSIT_DELAY,
    Disruption,
    ScenarioDefinition,
)

_SEED_PREFIX = "TL-"
_STOCK_SHORTAGE_REASON = "STOCK_SHORTAGE"


class TimelineSimulator:
    """Replays `SCENARIOS` against real repositories, one simulated day at a time."""

    def __init__(self, session: Session, end_date: date) -> None:
        self.session = session
        self.end_date = end_date
        self.timeline = FulfillmentTimelineRepository(session)
        self.risks = FulfillmentRiskRepository(session)
        self.alerts = TimelineAlertRepository(session)
        self.master_data = MasterDataRepository(session)
        self.purchase_orders = PurchaseOrderRepository(session)
        self.rules = PenaltyRuleRepository(session)
        self.retailer_agreements = RetailerAgreementRepository(session)
        self.service = TimelineProjectionService(
            timeline=self.timeline,
            risks=self.risks,
            alerts=self.alerts,
            purchase_orders=self.purchase_orders,
            rules=self.rules,
            master_data=self.master_data,
        )
        self._supply = SupplyInputBuilder(timeline=self.timeline, purchase_orders=self.purchase_orders)
        self._master_data_setup = TimelineMasterData(
            master_data=self.master_data, retailer_agreements=self.retailer_agreements, rules=self.rules
        )
        self._plans: dict[str, dict] = {}
        self._definitions: list[MilestoneDefinition] | None = None

    def replay(self, start: date | None = None, end: date | None = None) -> None:
        """Rebuild seeder data from scratch, then simulate every day from `start` to `end`.

        Every call is a clean rebuild: it deletes any prior seeder-owned rows
        first (the same scoped `reset()` used standalone), so calling `replay()`
        twice in a row -- with no `reset()` in between -- is idempotent instead
        of crashing on the scenario POs' duplicate business keys.
        """
        self.reset()
        end = end or self.end_date
        start = start or (end - timedelta(days=21))
        self._master_data_setup.setup(end)
        self._definitions = self.timeline.list_milestone_definitions()

        scenarios_by_order_date: dict[date, list[ScenarioDefinition]] = {}
        for scenario in SCENARIOS:
            order_date = end + timedelta(days=scenario.order_offset)
            scenarios_by_order_date.setdefault(order_date, []).append(scenario)

        current = start
        while current <= end:
            for scenario in scenarios_by_order_date.get(current, []):
                self._create_scenario(scenario, end)
            self.simulate_day(current)
            self.service.run_for_all_open(current)
            current += timedelta(days=1)

    def simulate_day(self, current: date) -> None:
        """One simulated day: disruptions, upstream supply, then milestone advancement."""
        self._apply_disruptions(current)
        self._complete_production_orders(current)
        self._release_quality_lots(current)
        self._advance_plans(current)

    def reset(self) -> None:
        """Delete every row this seeder owns (`TL-` business keys and their children).

        Deletion order respects every foreign key: alerts (by plan), risks/
        options (by PO), plans/milestones/events/production-orders/QA-lots,
        purchase orders, penalty rules, retailer agreements (by the
        agreement's own contract code, never by retailer id), then plants/
        materials/carriers/retailers -- the last step only ever removes a
        `TL-`-coded retailer, so a retailer reused by name from other seed
        data is never touched.
        """
        self.alerts.delete_seed_data(_SEED_PREFIX)
        self.risks.delete_seed_data(_SEED_PREFIX)
        self.timeline.delete_seed_data(_SEED_PREFIX, _SEED_PREFIX, _SEED_PREFIX)
        self.purchase_orders.delete_seed_data(_SEED_PREFIX)
        self.rules.delete_seed_data(_SEED_PREFIX)
        self.retailer_agreements.delete_seed_data(_SEED_PREFIX)
        self.master_data.delete_seed_data(_SEED_PREFIX, _SEED_PREFIX, _SEED_PREFIX, _SEED_PREFIX)
        self.session.commit()

    def _create_scenario(self, scenario: ScenarioDefinition, end: date) -> None:
        """Create one scenario's PO, plan, lines, baseline milestones, and upstream supply facts."""
        assert self._definitions is not None
        retailer_id = self._master_data_setup.retailer_ids[scenario.retailer_code]
        order_date = end + timedelta(days=scenario.order_offset)
        window_start = end + timedelta(days=scenario.window_start_offset)
        window_end = end + timedelta(days=scenario.window_end_offset)
        cancel_date = (
            end + timedelta(days=scenario.cancel_date_offset)
            if scenario.cancel_date_offset is not None
            else None
        )

        purchase_order = self.purchase_orders.create_purchase_order(
            purchase_order_number=f"{scenario.code}-PO",
            retailer_id=retailer_id,
            order_date=order_date,
            window_start=window_start,
            window_end=window_end,
            cancel_date=cancel_date,
            freight_term=scenario.freight_term,
        )
        plan = self.timeline.create_plan(
            plan_number=f"{scenario.code}-PLAN",
            purchase_order_id=purchase_order["id"],
            freight_term=scenario.freight_term,
            planned_transit_days=scenario.planned_transit_days,
        )

        for i, line in enumerate(scenario.lines):
            material_id = self._master_data_setup.material_ids[line.material_code]
            plant_id = self._master_data_setup.plant_ids[line.plant_code]
            po_line = self.purchase_orders.add_line(
                purchase_order_id=purchase_order["id"],
                line_number=str(10 * (i + 1)),
                ordered_quantity=line.quantity,
                unit_price=line.unit_price,
                material_id=material_id,
                plant_id=plant_id,
            )
            self.timeline.add_plan_line(plan["id"], po_line["id"], planned_quantity=line.quantity)

        for material_code, plant_code in scenario.initial_on_hand:
            quantity = scenario.initial_on_hand[(material_code, plant_code)]
            material_id = self._master_data_setup.material_ids[material_code]
            plant_id = self._master_data_setup.plant_ids[plant_code]
            self.timeline.adjust_available_quantity(material_id, plant_id, quantity)

        baseline_dates = backward_schedule(
            self._definitions,
            scenario.freight_term,
            order_date,
            window_start,
            window_end,
            scenario.planned_transit_days,
        )
        for code, milestone_date in baseline_dates.items():
            self.timeline.upsert_milestone(
                plan["id"], code, baseline_date=milestone_date, planned_date=milestone_date
            )

        for i, production_order in enumerate(scenario.production_orders):
            self.timeline.create_production_order(
                production_order_number=f"{scenario.code}-PROD-{i}",
                material_id=self._master_data_setup.material_ids[production_order.material_code],
                plant_id=self._master_data_setup.plant_ids[production_order.plant_code],
                planned_quantity=production_order.quantity,
                planned_end_date=end + timedelta(days=production_order.planned_end_offset),
                status="IN_PROGRESS",
            )

        for i, quality_lot in enumerate(scenario.quality_lots):
            self.timeline.create_quality_lot(
                lot_number=f"{scenario.code}-QA-{i}",
                material_id=self._master_data_setup.material_ids[quality_lot.material_code],
                plant_id=self._master_data_setup.plant_ids[quality_lot.plant_code],
                quantity=quality_lot.quantity,
                inspection_start_date=end + timedelta(days=quality_lot.inspection_start_offset),
                planned_release_date=end + timedelta(days=quality_lot.planned_release_offset),
                status="IN_INSPECTION",
            )

        self._plans[scenario.code] = plan

    def _apply_disruptions(self, current: date) -> None:
        """Apply every disruption dated `current`, across every scenario."""
        for scenario in SCENARIOS:
            plan = self._plans.get(scenario.code)
            if plan is None:
                continue
            for disruption in scenario.disruptions:
                disruption_date = self.end_date + timedelta(days=disruption.day_offset)
                if disruption_date != current:
                    continue
                if disruption.kind == MILESTONE_REPLAN:
                    self._replan_milestone(plan, disruption, current)
                elif disruption.kind == TRANSIT_DELAY:
                    self._replan_transit(plan, disruption, current)
                elif disruption.kind == PRODUCTION_REPLAN:
                    self._replan_production(scenario, disruption, current)
                elif disruption.kind == QA_HOLD:
                    self._replan_quality_lot(scenario, disruption, current)

    def _replan_milestone(self, plan: dict, disruption: Disruption, current: date) -> None:
        """Shift one plan milestone's `planned_date` by `disruption.shift_days`."""
        assert disruption.milestone_code is not None
        milestones_by_code = {m["code"]: m for m in self.timeline.list_milestones(plan["id"])}
        milestone = milestones_by_code[disruption.milestone_code]
        old_date = milestone["planned_date"]
        new_date = old_date + timedelta(days=disruption.shift_days)
        self.timeline.upsert_milestone(plan["id"], disruption.milestone_code, planned_date=new_date)
        self._write_event(
            subject_type="PLAN_MILESTONE",
            subject_id=milestone["id"],
            fulfillment_plan_id=plan["id"],
            milestone_type_id=milestone["milestone_type_id"],
            old_value=old_date,
            new_value=new_date,
            reason_code=disruption.reason_code,
            current=current,
        )

    def _replan_transit(self, plan: dict, disruption: Disruption, current: date) -> None:
        """Change a plan's `planned_transit_days`, the "carrier transit leg" of `DELIVERED`.

        Unlike `_replan_milestone`, this changes the plan's own transit
        duration rather than a milestone's stored `planned_date` -- a
        transit change moves the *dependency-derived* candidate that
        `DELIVERED`'s projection floors against (`GOODS_ISSUED.actual_date +
        planned_transit_days`), which a `planned_date` edit alone cannot
        pull earlier once `GOODS_ISSUED` has already completed on schedule.
        """
        old_transit = plan["planned_transit_days"] or 0
        new_transit = old_transit + disruption.shift_days
        self.timeline.set_plan_transit_days(plan["id"], new_transit)
        milestone = {m["code"]: m for m in self.timeline.list_milestones(plan["id"])}["DELIVERED"]
        self._write_event(
            subject_type="PLAN_MILESTONE",
            subject_id=milestone["id"],
            fulfillment_plan_id=plan["id"],
            milestone_type_id=milestone["milestone_type_id"],
            old_value=current + timedelta(days=old_transit),
            new_value=current + timedelta(days=new_transit),
            reason_code=disruption.reason_code,
            current=current,
        )

    def _replan_production(self, scenario: ScenarioDefinition, disruption: Disruption, current: date) -> None:
        """Shift the scenario's (sole) production order's `planned_end_date`."""
        number = f"{scenario.code}-PROD-0"
        production_order = self.timeline.find_production_order_by_number(number)
        assert production_order is not None
        old_date = production_order["planned_end_date"]
        new_date = old_date + timedelta(days=disruption.shift_days)
        self.timeline.replan_production_order(production_order["id"], new_date)
        self._write_event(
            subject_type="PRODUCTION_ORDER",
            subject_id=production_order["id"],
            fulfillment_plan_id=None,
            milestone_type_id=None,
            old_value=old_date,
            new_value=new_date,
            reason_code=disruption.reason_code,
            current=current,
        )

    def _replan_quality_lot(
        self, scenario: ScenarioDefinition, disruption: Disruption, current: date
    ) -> None:
        """Shift the scenario's (sole) QA lot's `planned_release_date`."""
        number = f"{scenario.code}-QA-0"
        quality_lot = self.timeline.find_quality_lot_by_number(number)
        assert quality_lot is not None
        old_date = quality_lot["planned_release_date"]
        new_date = old_date + timedelta(days=disruption.shift_days)
        self.timeline.replan_quality_lot(quality_lot["id"], new_date)
        self._write_event(
            subject_type="QA_LOT",
            subject_id=quality_lot["id"],
            fulfillment_plan_id=None,
            milestone_type_id=None,
            old_value=old_date,
            new_value=new_date,
            reason_code=disruption.reason_code,
            current=current,
        )

    def _write_event(
        self,
        *,
        subject_type: str,
        subject_id: UUID,
        fulfillment_plan_id: UUID | None,
        milestone_type_id: UUID | None,
        old_value: date,
        new_value: date,
        reason_code: str | None,
        current: date,
        event_type: str = "PLAN_CHANGED",
    ) -> None:
        self.timeline.add_event(
            subject_type=subject_type,
            subject_id=subject_id,
            fulfillment_plan_id=fulfillment_plan_id,
            milestone_type_id=milestone_type_id,
            event_type=event_type,
            old_value=old_value.isoformat(),
            new_value=new_value.isoformat(),
            reason_code=reason_code,
            event_at=datetime(current.year, current.month, current.day, tzinfo=UTC),
            source="SIMULATOR",
        )

    def _complete_production_orders(self, current: date) -> None:
        for row in self.timeline.list_production_orders_due(current, _SEED_PREFIX):
            produced = row["planned_quantity"] or 0.0
            self.timeline.complete_production_order(row["id"], produced, current)
            planned_release_date = current + timedelta(days=QA_RELEASE_DAYS)
            quality_lot = self.timeline.create_quality_lot(
                lot_number=f"{row['production_order_number']}-QA",
                production_order_id=row["id"],
                material_id=row["material_id"],
                plant_id=row["plant_id"],
                quantity=produced,
                inspection_start_date=current,
                planned_release_date=planned_release_date,
                status="IN_INSPECTION",
            )
            self._carry_forward_production_delay(row["id"], quality_lot, planned_release_date, current)

    def _carry_forward_production_delay(
        self, production_order_id: UUID, quality_lot: dict, planned_release_date: date, current: date
    ) -> None:
        """Thread a delayed production order's reason onto its freshly-created QA lot.

        The supply engine's cause attribution (`supply_inputs._baseline_and_reason`)
        reads a receipt's *own* event history: once a production order
        completes it drops out of `list_open_production_orders` and only its
        QA lot remains as the receipt, so a QA lot minted with no event of
        its own would otherwise lose the reason a delayed production run
        caused it to arrive late.
        """
        events = self.timeline.list_events_for_subject("PRODUCTION_ORDER", production_order_id)
        reasoned_events = [e for e in events if e["reason_code"] is not None]
        if not reasoned_events:
            return
        earliest = min(reasoned_events, key=lambda e: e["event_at"])
        original_end_date = date.fromisoformat(earliest["old_value"])
        self.timeline.add_event(
            subject_type="QA_LOT",
            subject_id=quality_lot["id"],
            fulfillment_plan_id=None,
            milestone_type_id=None,
            event_type="PLAN_CHANGED",
            old_value=(original_end_date + timedelta(days=QA_RELEASE_DAYS)).isoformat(),
            new_value=planned_release_date.isoformat(),
            reason_code=reasoned_events[-1]["reason_code"],
            event_at=datetime(current.year, current.month, current.day, tzinfo=UTC),
            source="SIMULATOR",
        )

    def _release_quality_lots(self, current: date) -> None:
        for row in self.timeline.list_quality_lots_due(current, _SEED_PREFIX):
            self.timeline.release_quality_lot(row["id"], current)
            self.timeline.adjust_available_quantity(row["material_id"], row["plant_id"], row["quantity"])

    def _advance_plans(self, current: date) -> None:
        """Close out plans DELIVERED on a prior day, then advance every still-open plan.

        Closing out is a separate, leading pass so a plan whose `DELIVERED`
        completes *today* stays `SHIPPED` (and so still `list_open_plans`-
        visible) through today's own projection run -- the run that must
        see the just-completed, possibly-late milestone to record a
        `BREACHED` risk. Only the next day's pass, once that risk is
        already durable, flips its status to `DELIVERED` and drops it from
        every later run.
        """
        assert self._definitions is not None
        for plan in self.timeline.list_open_plans():
            self._close_out_if_delivered(plan, current)
        for plan in self.timeline.list_open_plans():
            self._advance_one_plan(plan, current)

    def _close_out_if_delivered(self, plan: dict, current: date) -> None:
        milestones_by_code = {m["code"]: m for m in self.timeline.list_milestones(plan["id"])}
        delivered = milestones_by_code.get("DELIVERED") or milestones_by_code.get("READY_FOR_PICKUP")
        if delivered is None or delivered["actual_date"] is None:
            return
        if delivered["actual_date"] < current:
            self.timeline.set_plan_status(plan["id"], "DELIVERED")

    def _advance_one_plan(self, plan: dict, current: date) -> None:
        assert self._definitions is not None
        applicable = [d for d in self._definitions if d.freight_term_scope in ("ANY", plan["freight_term"])]
        applicable.sort(key=lambda d: d.sequence_no)
        applicable_codes = {d.code for d in applicable}
        milestones_by_code = {m["code"]: m for m in self.timeline.list_milestones(plan["id"])}

        for definition in applicable:
            milestone = milestones_by_code.get(definition.code)
            if milestone is None or milestone["actual_date"] is not None:
                continue
            if milestone["planned_date"] is None or milestone["planned_date"] > current:
                continue
            deps_done = all(
                milestones_by_code[dep]["actual_date"] is not None
                for dep in definition.depends_on
                if dep in applicable_codes
            )
            if not deps_done:
                continue

            if definition.code == "MATERIAL_AVAILABLE":
                self._complete_material_available(plan, milestone, current, milestones_by_code)
            elif definition.code == "GOODS_ISSUED":
                self._complete_goods_issued(plan, milestone, current, milestones_by_code)
            else:
                self._complete_milestone(plan, milestone, current, milestones_by_code)

    def _complete_milestone(
        self, plan: dict, milestone: dict, current: date, milestones_by_code: dict
    ) -> None:
        """Mark one milestone DONE and record a plain, reason-less `COMPLETED` fact.

        Reason-less on purpose: a later disruption-reason lookup
        (`latest_reasoned_event_for_milestone`) must never see this event as
        "the reason" the milestone slipped.
        """
        self.timeline.upsert_milestone(plan["id"], milestone["code"], actual_date=current, status="DONE")
        milestones_by_code[milestone["code"]]["actual_date"] = current
        self._write_event(
            subject_type="PLAN_MILESTONE",
            subject_id=milestone["id"],
            fulfillment_plan_id=plan["id"],
            milestone_type_id=milestone["milestone_type_id"],
            old_value=current,
            new_value=current,
            reason_code=None,
            current=current,
            event_type="COMPLETED",
        )

    def _complete_material_available(
        self, plan: dict, milestone: dict, current: date, milestones_by_code: dict
    ) -> None:
        enriched_lines = self._supply.enrich_lines(plan["id"])
        pools = {(line["material_id"], line["plant_id"]) for line in enriched_lines if line["material_id"]}
        if not pools:
            self._complete_milestone(plan, milestone, current, milestones_by_code)
            return

        coverage = [self._pool_coverage(pool, enriched_lines) for pool in pools]
        if all(free_on_hand >= remaining for free_on_hand, remaining in coverage):
            self._complete_milestone(plan, milestone, current, milestones_by_code)
            return

        if len(pools) > 1:
            raise NotImplementedError(
                f"Plan {plan['id']} MATERIAL_AVAILABLE spans {len(pools)} supply pools with "
                "a shortage in at least one; partial allocation across multiple pools is "
                "not implemented."
            )

        (free_on_hand, remaining) = coverage[0]
        if free_on_hand > 0:
            self._complete_material_available_partially(
                plan, milestone, current, milestones_by_code, free_on_hand, remaining
            )
            return

        self._reject_material_available_for_stock_shortage(plan, milestone, current)

    def _pool_coverage(self, pool: tuple[UUID, UUID], enriched_lines: list[dict]) -> tuple[float, float]:
        """Free on-hand stock and total demand for one (material, plant) pool's own lines."""
        material_id, plant_id = pool
        on_hand = self.timeline.get_on_hand(material_id, plant_id)
        reserved = self.timeline.sum_reserved_quantity(material_id, plant_id)
        remaining = sum(
            line["planned_quantity"]
            for line in enriched_lines
            if (line["material_id"], line["plant_id"]) == pool
        )
        return on_hand - reserved, remaining

    def _complete_material_available_partially(
        self,
        plan: dict,
        milestone: dict,
        current: date,
        milestones_by_code: dict,
        free_on_hand: float,
        remaining: float,
    ) -> None:
        """Allocate `free_on_hand` across every plan line, in line order, then complete the milestone."""
        left = free_on_hand
        for line in self.timeline.list_plan_lines(plan["id"]):
            allocated = max(0.0, min(left, line["planned_quantity"]))
            self.timeline.set_plan_line_quantities(line["id"], confirmed_quantity=allocated)
            left -= allocated

        self.timeline.upsert_milestone(plan["id"], "MATERIAL_AVAILABLE", actual_date=current, status="DONE")
        milestones_by_code["MATERIAL_AVAILABLE"]["actual_date"] = current
        self.timeline.add_event(
            subject_type="PLAN_MILESTONE",
            subject_id=milestone["id"],
            fulfillment_plan_id=plan["id"],
            milestone_type_id=milestone["milestone_type_id"],
            event_type="QTY_CHANGED",
            old_value=str(remaining),
            new_value=str(free_on_hand),
            reason_code=_STOCK_SHORTAGE_REASON,
            event_at=datetime(current.year, current.month, current.day, tzinfo=UTC),
            source="SIMULATOR",
        )

    def _reject_material_available_for_stock_shortage(
        self, plan: dict, milestone: dict, current: date
    ) -> None:
        """Leave `MATERIAL_AVAILABLE` pending with zero free stock: no daily replan.

        A daily +1 `planned_date` replan would keep dragging the supply
        engine's own need-date forward, permanently chasing whatever the
        blocked upstream receipt's `available_date` is. Instead the
        milestone simply stays put, and a one-time `REJECTED` event records
        the day the shortage was first hit -- written only once, so a
        still-blocked plan doesn't accumulate a duplicate event every day.
        """
        already_rejected = any(
            event["event_type"] == "REJECTED" and event["reason_code"] == _STOCK_SHORTAGE_REASON
            for event in self.timeline.list_events_for_subject("PLAN_MILESTONE", milestone["id"])
        )
        if already_rejected:
            return
        self._write_event(
            subject_type="PLAN_MILESTONE",
            subject_id=milestone["id"],
            fulfillment_plan_id=plan["id"],
            milestone_type_id=milestone["milestone_type_id"],
            old_value=current,
            new_value=current,
            reason_code=_STOCK_SHORTAGE_REASON,
            current=current,
            event_type="REJECTED",
        )

    def _complete_goods_issued(
        self, plan: dict, milestone: dict, current: date, milestones_by_code: dict
    ) -> None:
        for line in self.timeline.list_plan_lines(plan["id"]):
            po_line = self.purchase_orders.get_line(line["purchase_order_line_id"])
            confirmed = line["confirmed_quantity"]
            shipped = confirmed if confirmed is not None else line["planned_quantity"]
            self.timeline.set_plan_line_quantities(line["id"], shipped_quantity=shipped)
            if po_line is not None and po_line["material_id"] is not None:
                self.timeline.adjust_available_quantity(po_line["material_id"], po_line["plant_id"], -shipped)
        self._complete_milestone(plan, milestone, current, milestones_by_code)
        self.timeline.set_plan_status(plan["id"], "SHIPPED")
