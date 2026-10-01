"""Gathers supply-side inputs for the timeline engine: pools, demand, and upstream receipts.

Collaborator used by `TimelineProjectionService`: resolves which (material,
plant) pools a plan's lines draw from, joins plan lines to their PO line
details, and allocates on-hand stock plus upstream receipts (production
orders, QA lots) against every open plan's competing demand for those pools.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.services.penalties.timeline.supply import (
    PlanSupplyOutcome,
    SupplyDemand,
    SupplyReceipt,
    allocate_supply,
    plan_outcomes,
)


@dataclass
class SupplyInputBuilder:
    timeline: FulfillmentTimelineRepository
    purchase_orders: PurchaseOrderRepository

    def pools_for_plan(self, plan: dict) -> set[tuple[UUID, UUID]]:
        """Resolve the (material, plant) supply pools this plan's lines draw from."""
        pools: set[tuple[UUID, UUID]] = set()
        for line in self.enrich_lines(plan["id"]):
            pool = self._resolve_pool(plan, line)
            if pool is not None:
                pools.add(pool)
        return pools

    def enrich_lines(self, plan_id: UUID) -> list[dict]:
        """Plan lines joined with their PO line's material/plant/unit_price."""
        enriched = []
        for line in self.timeline.list_plan_lines(plan_id):
            po_line = self.purchase_orders.get_line(line["purchase_order_line_id"])
            if po_line is None:
                continue
            enriched.append(
                {
                    **line,
                    "material_id": po_line["material_id"],
                    "plant_id": po_line["plant_id"],
                    "unit_price": po_line["unit_price"],
                }
            )
        return enriched

    def compute_supply_outcomes(
        self, pools: set[tuple[UUID, UUID]], projection_date: date
    ) -> tuple[dict[str, PlanSupplyOutcome], dict[str, str]]:
        """Allocate supply for `pools` against every open plan's competing demand.

        Returns the resulting outcome per plan id, and a receipt source id ->
        subject_type map so a driver event on the cause source can be looked
        up later.
        """
        open_plans = self.timeline.list_open_plans()
        demands_by_pool: dict[tuple[UUID, UUID], list[SupplyDemand]] = defaultdict(list)
        for plan in open_plans:
            milestones_by_code = {m["code"]: m for m in self.timeline.list_milestones(plan["id"])}
            goods_issued = milestones_by_code.get("GOODS_ISSUED")
            if goods_issued is not None and goods_issued["actual_date"] is not None:
                continue
            material_available = milestones_by_code.get("MATERIAL_AVAILABLE")
            need_date = material_available["planned_date"] if material_available else None
            if need_date is None:
                continue
            purchase_order = self.purchase_orders.get_purchase_order(plan["purchase_order_id"])
            order_date = purchase_order["order_date"] if purchase_order else need_date

            for line in self.enrich_lines(plan["id"]):
                pool = self._resolve_pool(plan, line)
                if pool is None or pool not in pools:
                    continue
                confirmed = line["confirmed_quantity"]
                quantity = confirmed if confirmed is not None else line["planned_quantity"]
                demands_by_pool[pool].append(
                    SupplyDemand(
                        plan_id=str(plan["id"]),
                        plan_line_id=str(line["id"]),
                        quantity=quantity,
                        need_date=need_date,
                        order_date=order_date,
                        plan_number=plan["plan_number"],
                    )
                )

        outcomes: dict[str, PlanSupplyOutcome] = {}
        cause_source_types: dict[str, str] = {}
        for material_id, plant_id in pools:
            on_hand = self.timeline.get_on_hand(material_id, plant_id)
            receipts, pool_source_types = self._build_receipts(material_id, plant_id)
            cause_source_types.update(pool_source_types)
            coverages = allocate_supply(
                on_hand, receipts, demands_by_pool.get((material_id, plant_id), []), projection_date
            )
            for outcome in plan_outcomes(coverages):
                outcomes[outcome.plan_id] = outcome
        return outcomes, cause_source_types

    def _resolve_pool(self, plan: dict, line: dict) -> tuple[UUID, UUID] | None:
        """Resolve one plan line's (material, plant) pool: its own plant, else the plan's warehouse plant."""
        if line["material_id"] is None:
            return None
        plant_id = line["plant_id"]
        if plant_id is None and plan["ship_from_warehouse_id"] is not None:
            plant_id = self.timeline.get_warehouse_plant_id(plan["ship_from_warehouse_id"])
        if plant_id is None:
            return None
        return (line["material_id"], plant_id)

    def _build_receipts(
        self, material_id: UUID, plant_id: UUID
    ) -> tuple[list[SupplyReceipt], dict[str, str]]:
        """Upstream receipts for one (material, plant) pool: open production orders and QA lots."""
        receipts: list[SupplyReceipt] = []
        source_types: dict[str, str] = {}
        material_master = self.timeline.get_material_master(material_id, plant_id)
        qa_release_days = material_master["qa_release_days"] if material_master else None

        for order in self.timeline.list_open_production_orders(material_id, plant_id):
            if order["planned_end_date"] is None:
                continue
            available_date = order["planned_end_date"] + timedelta(days=qa_release_days or 0)
            events = self.timeline.list_events_for_subject("PRODUCTION_ORDER", order["id"])
            baseline, reason_code = self._baseline_and_reason(events, available_date, qa_release_days or 0)
            receipts.append(
                SupplyReceipt(
                    source_type="PRODUCTION_ORDER",
                    source_id=str(order["id"]),
                    quantity=order["planned_quantity"] or 0.0,
                    available_date=available_date,
                    baseline_available_date=baseline,
                    reason_code=reason_code,
                )
            )
            source_types[str(order["id"])] = "PRODUCTION_ORDER"

        for lot in self.timeline.list_open_quality_lots(material_id, plant_id):
            events = self.timeline.list_events_for_subject("QA_LOT", lot["id"])
            baseline, reason_code = self._baseline_and_reason(events, lot["planned_release_date"], 0)
            receipts.append(
                SupplyReceipt(
                    source_type="QA_LOT",
                    source_id=str(lot["id"]),
                    quantity=lot["quantity"],
                    available_date=lot["planned_release_date"],
                    baseline_available_date=baseline,
                    reason_code=reason_code,
                )
            )
            source_types[str(lot["id"])] = "QA_LOT"

        return receipts, source_types

    def _baseline_and_reason(
        self, events: list[dict], available_date: date, extra_days: int
    ) -> tuple[date, str | None]:
        """Earliest PLAN_CHANGED event's `old_value` as the baseline date, plus the latest event's reason."""
        plan_changed = next((e for e in events if e["event_type"] == "PLAN_CHANGED"), None)
        if plan_changed is not None and plan_changed["old_value"] is not None:
            baseline = date.fromisoformat(plan_changed["old_value"]) + timedelta(days=extra_days)
        else:
            baseline = available_date
        reason_code = events[-1]["reason_code"] if events else None
        return baseline, reason_code
