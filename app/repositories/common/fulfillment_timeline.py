"""Repository for the fulfillment-timeline schema: plans, milestones, events, upstream supply."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models import (
    FulfillmentEvent,
    FulfillmentMilestone,
    FulfillmentPlan,
    FulfillmentPlanLine,
    MaterialMaster,
    MilestoneType,
    ProductionOrder,
    PurchaseOrderLine,
    QualityLot,
    Warehouse,
)
from app.services.penalties.timeline.types import MilestoneDefinition


def _definition_from_row(row: MilestoneType) -> MilestoneDefinition:
    """Convert one `milestone_type` row into the pure engine's `MilestoneDefinition`."""
    return MilestoneDefinition(
        code=row.code,
        sequence_no=row.sequence_no,
        depends_on=tuple(row.depends_on),
        default_duration_days=(
            float(row.default_duration_days) if row.default_duration_days is not None else None
        ),
        freight_term_scope=row.freight_term_scope,
        is_measurement_point=row.is_measurement_point,
        owner_team=row.owner_team,
    )


def _plan_to_dict(row: FulfillmentPlan) -> dict:
    """Serialize a FulfillmentPlan row into a dict."""
    return {
        "id": row.id,
        "plan_number": row.plan_number,
        "purchase_order_id": row.purchase_order_id,
        "delivery_id": row.delivery_id,
        "ship_from_warehouse_id": row.ship_from_warehouse_id,
        "carrier_id": row.carrier_id,
        "freight_term": row.freight_term,
        "planned_transit_days": row.planned_transit_days,
        "status": row.status,
    }


def _plan_line_to_dict(row: FulfillmentPlanLine) -> dict:
    """Serialize a FulfillmentPlanLine row into a dict."""
    return {
        "id": row.id,
        "fulfillment_plan_id": row.fulfillment_plan_id,
        "purchase_order_line_id": row.purchase_order_line_id,
        "planned_quantity": float(row.planned_quantity),
        "confirmed_quantity": float(row.confirmed_quantity) if row.confirmed_quantity is not None else None,
        "shipped_quantity": float(row.shipped_quantity) if row.shipped_quantity is not None else None,
    }


def _milestone_to_dict(row: FulfillmentMilestone, code: str) -> dict:
    """Serialize a FulfillmentMilestone row (plus its resolved `milestone_type.code`) into a dict."""
    return {
        "id": row.id,
        "fulfillment_plan_id": row.fulfillment_plan_id,
        "milestone_type_id": row.milestone_type_id,
        "code": code,
        "baseline_date": row.baseline_date,
        "planned_date": row.planned_date,
        "actual_date": row.actual_date,
        "status": row.status,
        "source_document_type": row.source_document_type,
        "source_document_number": row.source_document_number,
    }


def _event_to_dict(row: FulfillmentEvent) -> dict:
    """Serialize a FulfillmentEvent row into a dict."""
    return {
        "id": row.id,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "fulfillment_plan_id": row.fulfillment_plan_id,
        "milestone_type_id": row.milestone_type_id,
        "event_type": row.event_type,
        "field_name": row.field_name,
        "old_value": row.old_value,
        "new_value": row.new_value,
        "reason_code": row.reason_code,
        "event_at": row.event_at,
        "source": row.source,
        "source_reference": row.source_reference,
    }


def _production_order_to_dict(row: ProductionOrder) -> dict:
    """Serialize a ProductionOrder row into a dict."""
    return {
        "id": row.id,
        "production_order_number": row.production_order_number,
        "material_id": row.material_id,
        "plant_id": row.plant_id,
        "planned_quantity": float(row.planned_quantity) if row.planned_quantity is not None else None,
        "produced_quantity": float(row.produced_quantity) if row.produced_quantity is not None else None,
        "planned_start_date": row.planned_start_date,
        "actual_start_date": row.actual_start_date,
        "planned_end_date": row.planned_end_date,
        "actual_end_date": row.actual_end_date,
        "status": row.status,
    }


def _quality_lot_to_dict(row: QualityLot) -> dict:
    """Serialize a QualityLot row into a dict."""
    return {
        "id": row.id,
        "lot_number": row.lot_number,
        "production_order_id": row.production_order_id,
        "material_id": row.material_id,
        "plant_id": row.plant_id,
        "quantity": float(row.quantity),
        "inspection_start_date": row.inspection_start_date,
        "planned_release_date": row.planned_release_date,
        "actual_release_date": row.actual_release_date,
        "status": row.status,
    }


def _material_master_to_dict(row: MaterialMaster) -> dict:
    """Serialize a MaterialMaster row into a dict, timeline-relevant fields only."""
    return {
        "id": row.id,
        "material_id": row.material_id,
        "plant_id": row.plant_id,
        "available_quantity": float(row.available_quantity) if row.available_quantity is not None else None,
        "standard_cost": float(row.standard_cost) if row.standard_cost is not None else None,
        "qa_release_days": row.qa_release_days,
    }


class FulfillmentTimelineRepository:
    """Access layer for the fulfillment-timeline schema (dict-returning, like `FulfillmentRepository`).

    Covers fulfillment plans, their lines and milestones, the append-only
    event trace, and the upstream supply facts (production orders, QA lots,
    on-hand stock) `TimelineProjectionService` allocates against.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._milestone_type_ids: dict[str, UUID] = {}

    def list_milestone_definitions(self) -> list[MilestoneDefinition]:
        """Return every `milestone_type` row as a pure engine `MilestoneDefinition`."""
        rows = self._session.scalars(select(MilestoneType).order_by(MilestoneType.sequence_no.asc())).all()
        return [_definition_from_row(r) for r in rows]

    def create_plan(
        self, plan_number: str, purchase_order_id: UUID, freight_term: str, **fields: Any
    ) -> dict:
        """Insert a fulfillment plan, keyed by `plan_number` (idempotent)."""
        existing = self._session.scalars(
            select(FulfillmentPlan).where(FulfillmentPlan.plan_number == plan_number)
        ).first()
        if existing is not None:
            return _plan_to_dict(existing)

        row = FulfillmentPlan(
            plan_number=plan_number, purchase_order_id=purchase_order_id, freight_term=freight_term, **fields
        )
        self._session.add(row)
        self._session.flush()
        return _plan_to_dict(row)

    def add_plan_line(
        self, fulfillment_plan_id: UUID, purchase_order_line_id: UUID, planned_quantity: float, **fields: Any
    ) -> dict:
        """Insert a plan line, keyed by (plan, PO line) (idempotent)."""
        existing = self._session.scalars(
            select(FulfillmentPlanLine).where(
                FulfillmentPlanLine.fulfillment_plan_id == fulfillment_plan_id,
                FulfillmentPlanLine.purchase_order_line_id == purchase_order_line_id,
            )
        ).first()
        if existing is not None:
            return _plan_line_to_dict(existing)

        row = FulfillmentPlanLine(
            fulfillment_plan_id=fulfillment_plan_id,
            purchase_order_line_id=purchase_order_line_id,
            planned_quantity=planned_quantity,
            **fields,
        )
        self._session.add(row)
        self._session.flush()
        return _plan_line_to_dict(row)

    def upsert_milestone(self, plan_id: UUID, code: str, **fields: Any) -> dict:
        """Create or update a plan's milestone state.

        `baseline_date` is written only while the row's current `baseline_date`
        is still `None` (first plan wins); every other field passed in
        `fields` is applied unconditionally.
        """
        milestone_type_id = self._milestone_type_id(code)
        existing = self._session.scalars(
            select(FulfillmentMilestone).where(
                FulfillmentMilestone.fulfillment_plan_id == plan_id,
                FulfillmentMilestone.milestone_type_id == milestone_type_id,
            )
        ).first()
        baseline_date = fields.pop("baseline_date", None)

        if existing is None:
            row = FulfillmentMilestone(
                fulfillment_plan_id=plan_id,
                milestone_type_id=milestone_type_id,
                baseline_date=baseline_date,
                **fields,
            )
            self._session.add(row)
        else:
            if existing.baseline_date is None and baseline_date is not None:
                existing.baseline_date = baseline_date
            for key, value in fields.items():
                setattr(existing, key, value)
            row = existing

        self._session.flush()
        return _milestone_to_dict(row, code)

    def add_event(self, **fields: Any) -> dict:
        """Append one fulfillment-event trace row (never idempotent: an audit trail)."""
        row = FulfillmentEvent(**fields)
        self._session.add(row)
        self._session.flush()
        return _event_to_dict(row)

    def get_plan(self, plan_id: UUID) -> dict | None:
        """Fetch one fulfillment plan by id, or None if not found."""
        row = self._session.get(FulfillmentPlan, plan_id)
        return _plan_to_dict(row) if row is not None else None

    def list_plans_for_purchase_order(self, purchase_order_id: UUID) -> list[dict]:
        """List every fulfillment plan for a purchase order."""
        rows = self._session.scalars(
            select(FulfillmentPlan).where(FulfillmentPlan.purchase_order_id == purchase_order_id)
        ).all()
        return [_plan_to_dict(r) for r in rows]

    def list_open_plans(self) -> list[dict]:
        """List every fulfillment plan with status OPEN or SHIPPED, oldest first.

        Deterministic ordering matters here: `TimelineProjectionService` commits
        per plan as it iterates this list, so the order decides which plans'
        rows are already durable if a later plan's processing fails.
        """
        rows = self._session.scalars(
            select(FulfillmentPlan)
            .where(FulfillmentPlan.status.in_(("OPEN", "SHIPPED")))
            .order_by(FulfillmentPlan.created_at.asc(), FulfillmentPlan.id.asc())
        ).all()
        return [_plan_to_dict(r) for r in rows]

    def list_plan_lines(self, plan_id: UUID) -> list[dict]:
        """List every line of one fulfillment plan, in creation (insertion) order.

        A stable order matters here: the simulator allocates a partial-stock
        shortage across lines "in line order", so this must not be left to
        whatever order the database happens to return.
        """
        rows = self._session.scalars(
            select(FulfillmentPlanLine)
            .where(FulfillmentPlanLine.fulfillment_plan_id == plan_id)
            .order_by(FulfillmentPlanLine.created_at.asc(), FulfillmentPlanLine.id.asc())
        ).all()
        return [_plan_line_to_dict(r) for r in rows]

    def list_milestones(self, plan_id: UUID) -> list[dict]:
        """List every milestone state for one plan, `MilestoneState`-compatible dicts plus `code`."""
        rows = self._session.execute(
            select(FulfillmentMilestone, MilestoneType.code)
            .join(MilestoneType, MilestoneType.id == FulfillmentMilestone.milestone_type_id)
            .where(FulfillmentMilestone.fulfillment_plan_id == plan_id)
        ).all()
        return [_milestone_to_dict(milestone, code) for milestone, code in rows]

    def list_events(self, plan_id: UUID) -> list[dict]:
        """List every event recorded for one plan, oldest first."""
        rows = self._session.scalars(
            select(FulfillmentEvent)
            .where(FulfillmentEvent.fulfillment_plan_id == plan_id)
            .order_by(FulfillmentEvent.event_at.asc())
        ).all()
        return [_event_to_dict(r) for r in rows]

    def latest_event_for_milestone(self, plan_id: UUID, code: str) -> dict | None:
        """Fetch the most recent event recorded against one plan milestone, or None."""
        milestone_type_id = self._milestone_type_id(code)
        row = self._session.scalars(
            select(FulfillmentEvent)
            .where(
                FulfillmentEvent.fulfillment_plan_id == plan_id,
                FulfillmentEvent.milestone_type_id == milestone_type_id,
            )
            .order_by(FulfillmentEvent.event_at.desc())
            .limit(1)
        ).first()
        return _event_to_dict(row) if row is not None else None

    def latest_reasoned_event_for_milestone(self, plan_id: UUID, code: str) -> dict | None:
        """Fetch the most recent *reason-carrying* event for one plan milestone, or None.

        Unlike `latest_event_for_milestone`, this skips a reason-less event
        (a plain `COMPLETED` fact) so a milestone's own completion never
        masks the disruption reason that actually caused its slip.
        """
        milestone_type_id = self._milestone_type_id(code)
        row = self._session.scalars(
            select(FulfillmentEvent)
            .where(
                FulfillmentEvent.fulfillment_plan_id == plan_id,
                FulfillmentEvent.milestone_type_id == milestone_type_id,
                FulfillmentEvent.reason_code.is_not(None),
            )
            .order_by(FulfillmentEvent.event_at.desc())
            .limit(1)
        ).first()
        return _event_to_dict(row) if row is not None else None

    def list_open_production_orders(self, material_id: UUID, plant_id: UUID) -> list[dict]:
        """List production orders for (material, plant) not yet COMPLETE."""
        rows = self._session.scalars(
            select(ProductionOrder).where(
                ProductionOrder.material_id == material_id,
                ProductionOrder.plant_id == plant_id,
                ProductionOrder.status != "COMPLETE",
            )
        ).all()
        return [_production_order_to_dict(r) for r in rows]

    def list_open_quality_lots(self, material_id: UUID, plant_id: UUID) -> list[dict]:
        """List QA lots for (material, plant) still IN_INSPECTION."""
        rows = self._session.scalars(
            select(QualityLot).where(
                QualityLot.material_id == material_id,
                QualityLot.plant_id == plant_id,
                QualityLot.status == "IN_INSPECTION",
            )
        ).all()
        return [_quality_lot_to_dict(r) for r in rows]

    def latest_event_for_subject(self, subject_type: str, subject_id: UUID) -> dict | None:
        """Fetch the most recent event recorded against one upstream subject, or None."""
        events = self.list_events_for_subject(subject_type, subject_id)
        return events[-1] if events else None

    def list_events_for_subject(self, subject_type: str, subject_id: UUID) -> list[dict]:
        """List every event recorded against one upstream subject, oldest first."""
        rows = self._session.scalars(
            select(FulfillmentEvent)
            .where(FulfillmentEvent.subject_type == subject_type, FulfillmentEvent.subject_id == subject_id)
            .order_by(FulfillmentEvent.event_at.asc())
        ).all()
        return [_event_to_dict(r) for r in rows]

    def get_on_hand(self, material_id: UUID, plant_id: UUID) -> float:
        """Return on-hand stock for (material, plant), 0.0 when unknown or unset."""
        material_master = self.get_material_master(material_id, plant_id)
        if material_master is None or material_master["available_quantity"] is None:
            return 0.0
        return material_master["available_quantity"]

    def get_material_master(self, material_id: UUID, plant_id: UUID) -> dict | None:
        """Fetch the (material, plant) master record, or None if not found."""
        row = self._session.scalars(
            select(MaterialMaster).where(
                MaterialMaster.material_id == material_id, MaterialMaster.plant_id == plant_id
            )
        ).first()
        return _material_master_to_dict(row) if row is not None else None

    def get_warehouse_plant_id(self, warehouse_id: UUID) -> UUID | None:
        """Resolve a warehouse's plant id, for the plan-line plant fallback."""
        return self._session.scalars(select(Warehouse.plant_id).where(Warehouse.id == warehouse_id)).first()

    def create_production_order(self, production_order_number: str, **fields: Any) -> dict:
        """Insert a production order, keyed by `production_order_number` (idempotent)."""
        existing = self._session.scalars(
            select(ProductionOrder).where(ProductionOrder.production_order_number == production_order_number)
        ).first()
        if existing is not None:
            return _production_order_to_dict(existing)

        row = ProductionOrder(production_order_number=production_order_number, **fields)
        self._session.add(row)
        self._session.flush()
        return _production_order_to_dict(row)

    def find_production_order_by_number(self, production_order_number: str) -> dict | None:
        """Fetch a production order by its business number, or None if not found."""
        row = self._session.scalars(
            select(ProductionOrder).where(ProductionOrder.production_order_number == production_order_number)
        ).first()
        return _production_order_to_dict(row) if row is not None else None

    def list_production_orders_due(self, current: date, number_prefix: str) -> list[dict]:
        """List not-yet-COMPLETE production orders due on `current`, scoped to `number_prefix`."""
        rows = self._session.scalars(
            select(ProductionOrder).where(
                ProductionOrder.production_order_number.like(f"{number_prefix}%"),
                ProductionOrder.status != "COMPLETE",
                ProductionOrder.planned_end_date == current,
            )
        ).all()
        return [_production_order_to_dict(r) for r in rows]

    def complete_production_order(
        self, production_order_id: UUID, produced_quantity: float, actual_end_date: date
    ) -> dict:
        """Mark a production order COMPLETE, its `produced_quantity`/`actual_end_date` set."""
        row = self._get_production_order_row(production_order_id)
        row.status = "COMPLETE"
        row.produced_quantity = produced_quantity
        row.actual_end_date = actual_end_date
        self._session.flush()
        return _production_order_to_dict(row)

    def replan_production_order(self, production_order_id: UUID, planned_end_date: date) -> dict:
        """Shift a production order's `planned_end_date`."""
        row = self._get_production_order_row(production_order_id)
        row.planned_end_date = planned_end_date
        self._session.flush()
        return _production_order_to_dict(row)

    def create_quality_lot(self, lot_number: str, **fields: Any) -> dict:
        """Insert a QA lot, keyed by `lot_number` (idempotent)."""
        existing = self._session.scalars(
            select(QualityLot).where(QualityLot.lot_number == lot_number)
        ).first()
        if existing is not None:
            return _quality_lot_to_dict(existing)

        row = QualityLot(lot_number=lot_number, **fields)
        self._session.add(row)
        self._session.flush()
        return _quality_lot_to_dict(row)

    def find_quality_lot_by_number(self, lot_number: str) -> dict | None:
        """Fetch a QA lot by its business number, or None if not found."""
        row = self._session.scalars(select(QualityLot).where(QualityLot.lot_number == lot_number)).first()
        return _quality_lot_to_dict(row) if row is not None else None

    def list_quality_lots_due(self, current: date, lot_number_prefix: str) -> list[dict]:
        """List IN_INSPECTION QA lots due for release on `current`, scoped to `lot_number_prefix`."""
        rows = self._session.scalars(
            select(QualityLot).where(
                QualityLot.lot_number.like(f"{lot_number_prefix}%"),
                QualityLot.status == "IN_INSPECTION",
                QualityLot.planned_release_date == current,
            )
        ).all()
        return [_quality_lot_to_dict(r) for r in rows]

    def release_quality_lot(self, quality_lot_id: UUID, actual_release_date: date) -> dict:
        """Mark a QA lot RELEASED, its `actual_release_date` set."""
        row = self._get_quality_lot_row(quality_lot_id)
        row.status = "RELEASED"
        row.actual_release_date = actual_release_date
        self._session.flush()
        return _quality_lot_to_dict(row)

    def replan_quality_lot(self, quality_lot_id: UUID, planned_release_date: date) -> dict:
        """Shift a QA lot's `planned_release_date`."""
        row = self._get_quality_lot_row(quality_lot_id)
        row.planned_release_date = planned_release_date
        self._session.flush()
        return _quality_lot_to_dict(row)

    def adjust_available_quantity(self, material_id: UUID, plant_id: UUID, delta: float) -> float:
        """Add `delta` (positive or negative) to a (material, plant)'s on-hand quantity."""
        row = self._session.scalars(
            select(MaterialMaster).where(
                MaterialMaster.material_id == material_id, MaterialMaster.plant_id == plant_id
            )
        ).first()
        if row is None:
            raise NotFoundError(
                code="MATERIAL_MASTER_NOT_FOUND",
                message=f"No material_master found for material_id={material_id}, plant_id={plant_id}",
            )
        row.available_quantity = float(row.available_quantity or 0.0) + float(delta)
        self._session.flush()
        return row.available_quantity

    def set_plan_status(self, plan_id: UUID, status: str) -> None:
        """Set a fulfillment plan's `status`."""
        row = self._get_plan_row(plan_id)
        row.status = status
        self._session.flush()

    def set_plan_transit_days(self, plan_id: UUID, planned_transit_days: int) -> None:
        """Set a fulfillment plan's `planned_transit_days` (a carrier transit-time replan)."""
        row = self._get_plan_row(plan_id)
        row.planned_transit_days = planned_transit_days
        self._session.flush()

    def set_plan_line_quantities(
        self,
        plan_line_id: UUID,
        confirmed_quantity: float | None = None,
        shipped_quantity: float | None = None,
    ) -> dict:
        """Set a plan line's `confirmed_quantity`/`shipped_quantity`, leaving unset args untouched."""
        row = self._session.get(FulfillmentPlanLine, plan_line_id)
        if row is None:
            raise NotFoundError(
                code="FULFILLMENT_PLAN_LINE_NOT_FOUND",
                message=f"No fulfillment plan line found with plan_line_id={plan_line_id}",
            )
        if confirmed_quantity is not None:
            row.confirmed_quantity = confirmed_quantity
        if shipped_quantity is not None:
            row.shipped_quantity = shipped_quantity
        self._session.flush()
        return _plan_line_to_dict(row)

    def sum_reserved_quantity(self, material_id: UUID, plant_id: UUID) -> float:
        """Sum the remaining (confirmed or planned) quantity reserved against one (material, plant) pool.

        A plan reserves supply once its own `MATERIAL_AVAILABLE` is done and
        stops reserving once `GOODS_ISSUED` is done: this is the "free
        on-hand" denominator `simulate_day`'s MATERIAL_AVAILABLE completion
        check subtracts from `material_master.available_quantity`.
        """
        total = 0.0
        for plan in self.list_open_plans():
            milestones_by_code = {m["code"]: m for m in self.list_milestones(plan["id"])}
            material_available = milestones_by_code.get("MATERIAL_AVAILABLE")
            goods_issued = milestones_by_code.get("GOODS_ISSUED")
            if material_available is None or material_available["actual_date"] is None:
                continue
            if goods_issued is not None and goods_issued["actual_date"] is not None:
                continue
            for line in self.list_plan_lines(plan["id"]):
                po_line = self._session.get(PurchaseOrderLine, line["purchase_order_line_id"])
                if po_line is None or po_line.material_id != material_id or po_line.plant_id != plant_id:
                    continue
                confirmed = line["confirmed_quantity"]
                total += confirmed if confirmed is not None else line["planned_quantity"]
        return total

    def delete_seed_data(
        self, plan_number_prefix: str, production_order_number_prefix: str, quality_lot_number_prefix: str
    ) -> None:
        """Delete every plan/milestone/event/production-order/QA-lot row owned by these prefixes."""
        plan_like = f"{plan_number_prefix}%"
        production_order_like = f"{production_order_number_prefix}%"
        quality_lot_like = f"{quality_lot_number_prefix}%"
        plan_ids = select(FulfillmentPlan.id).where(FulfillmentPlan.plan_number.like(plan_like))
        production_order_ids = select(ProductionOrder.id).where(
            ProductionOrder.production_order_number.like(production_order_like)
        )
        quality_lot_ids = select(QualityLot.id).where(QualityLot.lot_number.like(quality_lot_like))

        self._session.execute(
            delete(FulfillmentEvent).where(FulfillmentEvent.fulfillment_plan_id.in_(plan_ids))
        )
        self._session.execute(
            delete(FulfillmentEvent).where(
                FulfillmentEvent.subject_type == "PRODUCTION_ORDER",
                FulfillmentEvent.subject_id.in_(production_order_ids),
            )
        )
        self._session.execute(
            delete(FulfillmentEvent).where(
                FulfillmentEvent.subject_type == "QA_LOT", FulfillmentEvent.subject_id.in_(quality_lot_ids)
            )
        )
        self._session.execute(
            delete(FulfillmentMilestone).where(FulfillmentMilestone.fulfillment_plan_id.in_(plan_ids))
        )
        self._session.execute(
            delete(FulfillmentPlanLine).where(FulfillmentPlanLine.fulfillment_plan_id.in_(plan_ids))
        )
        self._session.execute(delete(FulfillmentPlan).where(FulfillmentPlan.plan_number.like(plan_like)))
        self._session.execute(delete(QualityLot).where(QualityLot.lot_number.like(quality_lot_like)))
        self._session.execute(
            delete(ProductionOrder).where(ProductionOrder.production_order_number.like(production_order_like))
        )
        self._session.flush()

    def _get_plan_row(self, plan_id: UUID) -> FulfillmentPlan:
        row = self._session.get(FulfillmentPlan, plan_id)
        if row is None:
            raise NotFoundError(
                code="FULFILLMENT_PLAN_NOT_FOUND", message=f"No fulfillment plan found with plan_id={plan_id}"
            )
        return row

    def _get_production_order_row(self, production_order_id: UUID) -> ProductionOrder:
        row = self._session.get(ProductionOrder, production_order_id)
        if row is None:
            raise NotFoundError(
                code="PRODUCTION_ORDER_NOT_FOUND",
                message=f"No production order found with production_order_id={production_order_id}",
            )
        return row

    def _get_quality_lot_row(self, quality_lot_id: UUID) -> QualityLot:
        row = self._session.get(QualityLot, quality_lot_id)
        if row is None:
            raise NotFoundError(
                code="QUALITY_LOT_NOT_FOUND", message=f"No QA lot found with quality_lot_id={quality_lot_id}"
            )
        return row

    def _milestone_type_id(self, code: str) -> UUID:
        """Resolve `code` to its `milestone_type.id`, caching hits for this repository instance."""
        cached = self._milestone_type_ids.get(code)
        if cached is not None:
            return cached

        milestone_type_id = self._session.scalars(
            select(MilestoneType.id).where(MilestoneType.code == code)
        ).first()
        if milestone_type_id is None:
            raise ValueError(f"No milestone_type found with code={code!r}")
        self._milestone_type_ids[code] = milestone_type_id
        return milestone_type_id
