"""Repository for common schema fulfillment facts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    Delivery,
    DeliveryLine,
    DemandException,
    OrderConfirmation,
    OrderConfirmationLine,
    ProductionOrder,
    ProductionSchedule,
    Shipment,
)


def _end_of_day(d: date) -> datetime:
    """Return the last instant of `d`, for an inclusive as-of-date comparison against a timestamp column."""
    return datetime.combine(d, datetime.max.time())


def _confirmation_to_dict(row: OrderConfirmation) -> dict:
    """Serialize an OrderConfirmation row into a dict."""
    return {
        "id": row.id,
        "confirmation_number": row.confirmation_number,
        "purchase_order_id": row.purchase_order_id,
        "confirmation_date": row.confirmation_date,
        "status": row.status,
    }


def _confirmation_line_to_dict(row: OrderConfirmationLine) -> dict:
    """Serialize an OrderConfirmationLine row into a dict."""
    return {
        "id": row.id,
        "order_confirmation_id": row.order_confirmation_id,
        "purchase_order_line_id": row.purchase_order_line_id,
        "confirmed_quantity": float(row.confirmed_quantity),
        "confirmed_delivery_date": row.confirmed_delivery_date,
        "cut_reason_code": row.cut_reason_code,
    }


def _delivery_to_dict(row: Delivery) -> dict:
    """Serialize a Delivery row into a dict."""
    return {
        "id": row.id,
        "delivery_number": row.delivery_number,
        "purchase_order_id": row.purchase_order_id,
        "ship_from_plant_id": row.ship_from_plant_id,
        "ship_from_warehouse_id": row.ship_from_warehouse_id,
        "ship_to_location_id": row.ship_to_location_id,
        "delivery_status": row.delivery_status,
        "planned_delivery_date": row.planned_delivery_date,
        "actual_delivery_date": row.actual_delivery_date,
        "planned_ship_date": row.planned_ship_date,
        "actual_ship_date": row.actual_ship_date,
        "goods_issue_date": row.goods_issue_date,
    }


def _delivery_line_to_dict(row: DeliveryLine) -> dict:
    """Serialize a DeliveryLine row into a dict."""
    return {
        "id": row.id,
        "delivery_id": row.delivery_id,
        "purchase_order_line_id": row.purchase_order_line_id,
        "delivered_quantity": float(row.delivered_quantity),
        "uom": row.uom,
        "status": row.status,
    }


def _shipment_to_dict(row: Shipment) -> dict:
    """Serialize a Shipment row into a dict."""
    return {
        "id": row.id,
        "shipment_number": row.shipment_number,
        "delivery_id": row.delivery_id,
        "carrier_id": row.carrier_id,
        "expected_ship_date": row.expected_ship_date,
        "actual_ship_date": row.actual_ship_date,
        "expected_delivery_date": row.expected_delivery_date,
        "actual_delivery_date": row.actual_delivery_date,
        "expected_transit_days": row.expected_transit_days,
        "appointment_status": row.appointment_status,
        "shipment_status": row.shipment_status,
        "recorded_at": row.recorded_at,
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


def _production_schedule_to_dict(row: ProductionSchedule) -> dict:
    """Serialize a ProductionSchedule row into a dict."""
    return {
        "id": row.id,
        "production_order_id": row.production_order_id,
        "material_id": row.material_id,
        "plant_id": row.plant_id,
        "scheduled_quantity": float(row.scheduled_quantity) if row.scheduled_quantity is not None else None,
        "scheduled_start_at": row.scheduled_start_at,
        "scheduled_end_at": row.scheduled_end_at,
        "status": row.status,
        "status_at": row.status_at,
    }


def _demand_exception_to_dict(row: DemandException) -> dict:
    """Serialize a DemandException row into a dict."""
    return {
        "id": row.id,
        "exception_id": row.exception_id,
        "purchase_order_line_id": row.purchase_order_line_id,
        "flagged_date": row.flagged_date,
        "resolved": row.resolved,
    }


class FulfillmentRepository:
    """Access layer for common schema fulfillment facts.

    Covers order confirmations, deliveries, shipments, production orders and
    schedules, and demand exceptions. Every `add_*` method goes through
    `_insert_if_absent` on the fact's natural key, so re-ingesting the same fact is
    a no-op.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _insert_if_absent(self, model: type, filters: dict[str, Any], **fields: Any):
        """Insert a fact only when its natural key does not already exist.

        `filters` holds the equality conditions identifying that key: a single
        unique column, or a composite tuple enforced only in application code for
        `production_schedule`, which has no unique constraint.
        """
        conditions = [getattr(model, key) == value for key, value in filters.items()]
        existing = self._session.scalars(select(model).where(*conditions)).first()
        if existing is not None:
            return existing

        row = model(**filters, **fields)
        self._session.add(row)
        self._session.flush()
        return row

    # ------------------------------------------------------------------
    # Order confirmation
    # ------------------------------------------------------------------

    def add_order_confirmation(
        self,
        confirmation_number: str,
        purchase_order_id: UUID,
        confirmation_date: datetime,
        status: str | None = None,
    ) -> dict:
        """Insert an order confirmation, keyed by `confirmation_number` (idempotent)."""
        row = self._insert_if_absent(
            OrderConfirmation,
            {"confirmation_number": confirmation_number},
            purchase_order_id=purchase_order_id,
            confirmation_date=confirmation_date,
            status=status,
        )
        return _confirmation_to_dict(row)

    def add_order_confirmation_line(
        self,
        order_confirmation_id: UUID,
        purchase_order_line_id: UUID,
        confirmed_quantity: float,
        confirmed_delivery_date: date | None = None,
        cut_reason_code: str | None = None,
    ) -> dict:
        """Insert an order confirmation line, keyed by (confirmation, PO line) (idempotent)."""
        row = self._insert_if_absent(
            OrderConfirmationLine,
            {
                "order_confirmation_id": order_confirmation_id,
                "purchase_order_line_id": purchase_order_line_id,
            },
            confirmed_quantity=confirmed_quantity,
            confirmed_delivery_date=confirmed_delivery_date,
            cut_reason_code=cut_reason_code,
        )
        return _confirmation_line_to_dict(row)

    def list_confirmation_lines_for_line(self, purchase_order_line_id: UUID) -> list[dict]:
        """Full history, oldest first (by the parent confirmation's date)."""
        rows = self._session.scalars(
            select(OrderConfirmationLine)
            .join(OrderConfirmation, OrderConfirmation.id == OrderConfirmationLine.order_confirmation_id)
            .where(OrderConfirmationLine.purchase_order_line_id == purchase_order_line_id)
            .order_by(OrderConfirmation.confirmation_date.asc())
        ).all()
        return [_confirmation_line_to_dict(r) for r in rows]

    def get_latest_confirmation_line_not_after(
        self, purchase_order_line_id: UUID, as_of_date: date
    ) -> dict | None:
        """Fetch the latest confirmation line for a PO line as of a historical date, or None if none exist."""
        row = self._session.scalars(
            select(OrderConfirmationLine)
            .join(OrderConfirmation, OrderConfirmation.id == OrderConfirmationLine.order_confirmation_id)
            .where(
                OrderConfirmationLine.purchase_order_line_id == purchase_order_line_id,
                OrderConfirmation.confirmation_date <= _end_of_day(as_of_date),
            )
            .order_by(OrderConfirmation.confirmation_date.desc())
            .limit(1)
        ).first()
        return _confirmation_line_to_dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Delivery / shipment
    # ------------------------------------------------------------------

    def add_delivery(self, delivery_number: str, purchase_order_id: UUID, **fields: Any) -> dict:
        """Insert a delivery, keyed by `delivery_number` (idempotent)."""
        row = self._insert_if_absent(
            Delivery, {"delivery_number": delivery_number}, purchase_order_id=purchase_order_id, **fields
        )
        return _delivery_to_dict(row)

    def add_delivery_line(
        self, delivery_id: UUID, purchase_order_line_id: UUID, delivered_quantity: float, **fields: Any
    ) -> dict:
        """Insert a delivery line, keyed by (delivery, PO line) (idempotent)."""
        row = self._insert_if_absent(
            DeliveryLine,
            {"delivery_id": delivery_id, "purchase_order_line_id": purchase_order_line_id},
            delivered_quantity=delivered_quantity,
            **fields,
        )
        return _delivery_line_to_dict(row)

    def list_deliveries_for_purchase_order(self, purchase_order_id: UUID) -> list[dict]:
        """List all deliveries for a PO, oldest first."""
        rows = self._session.scalars(
            select(Delivery)
            .where(Delivery.purchase_order_id == purchase_order_id)
            .order_by(Delivery.created_at.asc())
        ).all()
        return [_delivery_to_dict(r) for r in rows]

    def add_shipment(
        self,
        shipment_number: str,
        delivery_id: UUID,
        recorded_at: datetime,
        **fields: Any,
    ) -> dict:
        """Insert a shipment, keyed by `shipment_number` (idempotent)."""
        row = self._insert_if_absent(
            Shipment,
            {"shipment_number": shipment_number},
            delivery_id=delivery_id,
            recorded_at=recorded_at,
            **fields,
        )
        return _shipment_to_dict(row)

    def list_shipments_for_delivery(self, delivery_id: UUID) -> list[dict]:
        """List all shipments for a delivery, oldest first."""
        rows = self._session.scalars(
            select(Shipment).where(Shipment.delivery_id == delivery_id).order_by(Shipment.recorded_at.asc())
        ).all()
        return [_shipment_to_dict(r) for r in rows]

    def list_shipments_for_purchase_order(self, purchase_order_id: UUID) -> list[dict]:
        """Return the full shipment history for a PO, oldest first, across all deliveries."""
        rows = self._session.scalars(
            select(Shipment)
            .join(Delivery, Delivery.id == Shipment.delivery_id)
            .where(Delivery.purchase_order_id == purchase_order_id)
            .order_by(Shipment.recorded_at.asc())
        ).all()
        return [_shipment_to_dict(r) for r in rows]

    def get_delivered_quantity_for_purchase_order_not_after(
        self, purchase_order_id: UUID, as_of_date: date
    ) -> float | None:
        """Return the quantity actually delivered for a PO as of a historical date.

        Sums `DeliveryLine.delivered_quantity` over deliveries whose
        `actual_delivery_date` is set and not after `as_of_date`, so a delivery that
        has not physically completed contributes nothing. Unlike
        `get_latest_confirmation_line_not_after`, which returns the pre-delivery
        promise, this is what shipped. Returns `None`, never `0.0`, when no line
        qualifies: callers must read that as unknown, not as a confirmed zero.
        """
        total = self._session.scalar(
            select(func.sum(DeliveryLine.delivered_quantity))
            .select_from(DeliveryLine)
            .join(Delivery, Delivery.id == DeliveryLine.delivery_id)
            .where(
                Delivery.purchase_order_id == purchase_order_id,
                Delivery.actual_delivery_date.isnot(None),
                Delivery.actual_delivery_date <= as_of_date,
            )
        )
        return float(total) if total is not None else None

    def get_latest_shipment_for_purchase_order_not_after(
        self, purchase_order_id: UUID, as_of_date: date
    ) -> dict | None:
        """Fetch the latest shipment for a PO as of a historical date, or None if none exist."""
        row = self._session.scalars(
            select(Shipment)
            .join(Delivery, Delivery.id == Shipment.delivery_id)
            .where(
                Delivery.purchase_order_id == purchase_order_id,
                Shipment.recorded_at <= _end_of_day(as_of_date),
            )
            .order_by(Shipment.recorded_at.desc())
            .limit(1)
        ).first()
        return _shipment_to_dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Production
    # ------------------------------------------------------------------

    def add_production_order(self, production_order_number: str, **fields: Any) -> dict:
        """Insert a production order, keyed by `production_order_number` (idempotent)."""
        row = self._insert_if_absent(
            ProductionOrder, {"production_order_number": production_order_number}, **fields
        )
        return _production_order_to_dict(row)

    def add_production_schedule(
        self,
        material_id: UUID,
        plant_id: UUID,
        status: str,
        status_at: datetime,
        production_order_id: UUID | None = None,
        scheduled_quantity: float | None = None,
        scheduled_start_at: datetime | None = None,
        scheduled_end_at: datetime | None = None,
    ) -> dict:
        """Insert a production schedule snapshot, keyed by (material, plant, status_at) (idempotent)."""
        row = self._insert_if_absent(
            ProductionSchedule,
            {"material_id": material_id, "plant_id": plant_id, "status_at": status_at},
            status=status,
            production_order_id=production_order_id,
            scheduled_quantity=scheduled_quantity,
            scheduled_start_at=scheduled_start_at,
            scheduled_end_at=scheduled_end_at,
        )
        return _production_schedule_to_dict(row)

    def list_production_schedule_for_material_plant(self, material_id: UUID, plant_id: UUID) -> list[dict]:
        """Return every schedule snapshot for a (material, plant) pair, oldest first.

        Deliberately not PO-scoped: one production line can serve several orders
        sharing the same material and plant.
        """
        rows = self._session.scalars(
            select(ProductionSchedule)
            .where(ProductionSchedule.material_id == material_id, ProductionSchedule.plant_id == plant_id)
            .order_by(ProductionSchedule.status_at.asc(), ProductionSchedule.id.asc())
        ).all()
        return [_production_schedule_to_dict(r) for r in rows]

    def get_latest_production_schedule_not_after(
        self, material_id: UUID, plant_id: UUID, as_of_date: date
    ) -> dict | None:
        """Fetch the latest production schedule for (material, plant) not after a date."""
        # Two rows can share a status_at (a material/plant serves more than one
        # order, and mock scenarios may write several statuses for one day), so id
        # breaks the tie deterministically.
        row = self._session.scalars(
            select(ProductionSchedule)
            .where(
                ProductionSchedule.material_id == material_id,
                ProductionSchedule.plant_id == plant_id,
                ProductionSchedule.status_at <= _end_of_day(as_of_date),
            )
            .order_by(ProductionSchedule.status_at.desc(), ProductionSchedule.id.desc())
            .limit(1)
        ).first()
        return _production_schedule_to_dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Demand exception
    # ------------------------------------------------------------------

    def add_demand_exception(
        self, exception_id: str, purchase_order_line_id: UUID, flagged_date: date
    ) -> dict:
        """Insert a demand exception, keyed by `exception_id` (idempotent)."""
        row = self._insert_if_absent(
            DemandException,
            {"exception_id": exception_id},
            purchase_order_line_id=purchase_order_line_id,
            flagged_date=flagged_date,
            resolved=False,
        )
        return _demand_exception_to_dict(row)

    def list_demand_exceptions_for_line(self, purchase_order_line_id: UUID) -> list[dict]:
        """List all demand exceptions for a PO line, oldest first."""
        rows = self._session.scalars(
            select(DemandException)
            .where(DemandException.purchase_order_line_id == purchase_order_line_id)
            .order_by(DemandException.flagged_date.asc())
        ).all()
        return [_demand_exception_to_dict(r) for r in rows]

    def has_open_demand_exception_not_after(self, purchase_order_line_id: UUID, as_of_date: date) -> bool:
        """Report whether a PO line has an unresolved demand exception flagged by `as_of_date`."""
        row = self._session.scalars(
            select(DemandException)
            .where(
                DemandException.purchase_order_line_id == purchase_order_line_id,
                DemandException.resolved.is_(False),
                DemandException.flagged_date <= as_of_date,
            )
            .limit(1)
        ).first()
        return row is not None

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def truncate_all(self) -> None:
        """Delete every fulfillment fact, child before parent, ahead of PO truncation."""
        self._session.execute(delete(OrderConfirmationLine))
        self._session.execute(delete(OrderConfirmation))
        self._session.execute(delete(Shipment))
        self._session.execute(delete(DeliveryLine))
        self._session.execute(delete(Delivery))
        self._session.execute(delete(ProductionSchedule))
        self._session.execute(delete(ProductionOrder))
        self._session.execute(delete(DemandException))
        self._session.flush()
