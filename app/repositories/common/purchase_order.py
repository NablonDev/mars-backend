"""Repository for purchase-order header/line pairs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.models import PurchaseOrder, PurchaseOrderLine
from app.utils.pagination import next_cursor_from_page, parse_cursor


def describe_no_open_orders(counts: dict[str, int]) -> str | None:
    """Explain a zero-OPEN-purchase-order batch, or None when there is nothing to explain.

    A batch that enqueues nothing because every PO is DELIVERED looks identical to a
    broken one in the logs, so both callers say which it is.
    """
    if counts.get("OPEN") or not counts:
        return None

    breakdown = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
    return (
        f"No OPEN purchase orders to enqueue, but {sum(counts.values())} purchase order(s) exist "
        f"({breakdown}). Nothing will run until a purchase order is OPEN: re-seed, or reopen the "
        "existing purchase orders."
    )


def _purchase_order_to_dict(row: PurchaseOrder) -> dict:
    """Serialize a PurchaseOrder row into a dict."""
    return {
        "id": row.id,
        "purchase_order_number": row.purchase_order_number,
        "retailer_id": row.retailer_id,
        "retailer_po_number": row.retailer_po_number,
        "order_date": row.order_date,
        "requested_delivery_date": row.requested_delivery_date,
        "required_ship_date": row.required_ship_date,
        "order_status": row.order_status,
        "source_system": row.source_system,
        "source_document_type": row.source_document_type,
        "source_document_number": row.source_document_number,
        "current_delivery_date": row.current_delivery_date,
        "current_required_ship_date": row.current_required_ship_date,
        "negotiation_status": row.negotiation_status,
    }


def _purchase_order_line_to_dict(row: PurchaseOrderLine) -> dict:
    """Serialize a PurchaseOrderLine row into a dict."""
    return {
        "id": row.id,
        "purchase_order_id": row.purchase_order_id,
        "line_number": row.line_number,
        "retailer_po_line_number": row.retailer_po_line_number,
        "sku_id": row.sku_id,
        "material_id": row.material_id,
        "retailer_material_code": row.retailer_material_code,
        "plant_id": row.plant_id,
        "storage_location_id": row.storage_location_id,
        "ship_to_location_id": row.ship_to_location_id,
        "ordered_quantity": float(row.ordered_quantity),
        "unit_price": float(row.unit_price),
        "uom": row.uom,
        "requested_delivery_date": row.requested_delivery_date,
        "required_ship_date": row.required_ship_date,
        "line_status": row.line_status,
        "raw_payload": row.raw_payload,
        "updated_at": row.updated_at,
    }


class PurchaseOrderRepository:
    """Access layer for purchase_order and purchase_order_line facts.

    Also owns the PO's status transitions and the negotiation-lifecycle
    fields (`current_delivery_date`, `current_required_ship_date`,
    `negotiation_status`) that `PoDeliveryChangeRequestService` alone writes.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _get_row(self, purchase_order_id: UUID) -> PurchaseOrder | None:
        """Fetch the ORM row for a purchase order id, or None if not found."""
        return self._session.get(PurchaseOrder, purchase_order_id)

    def _get_row_by_number(self, purchase_order_number: str) -> PurchaseOrder | None:
        """Fetch the ORM row for a purchase order number, or None if not found."""
        return self._session.scalars(
            select(PurchaseOrder).where(PurchaseOrder.purchase_order_number == purchase_order_number)
        ).first()

    def _get_line_row(self, purchase_order_line_id: UUID) -> PurchaseOrderLine | None:
        """Fetch the ORM row for a purchase order line id, or None if not found."""
        return self._session.get(PurchaseOrderLine, purchase_order_line_id)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def create_purchase_order(self, purchase_order_number: str, **fields) -> dict:
        """Create a purchase order, raising `ConflictError` if `purchase_order_number` already exists."""
        if self._get_row_by_number(purchase_order_number) is not None:
            raise ConflictError(
                code="PO_ALREADY_EXISTS",
                message=f"Purchase order {purchase_order_number!r} already exists",
            )

        row = PurchaseOrder(purchase_order_number=purchase_order_number, **fields)
        self._session.add(row)
        self._session.flush()
        return _purchase_order_to_dict(row)

    def get_purchase_order(self, purchase_order_id: UUID) -> dict | None:
        """Fetch a purchase order by id, or None if not found."""
        row = self._get_row(purchase_order_id)
        return _purchase_order_to_dict(row) if row is not None else None

    def get_by_number(self, purchase_order_number: str) -> dict | None:
        """Fetch a purchase order by its business number, or None if not found."""
        row = self._get_row_by_number(purchase_order_number)
        return _purchase_order_to_dict(row) if row is not None else None

    def require_purchase_order(self, purchase_order_id: UUID) -> dict:
        """Fetch a purchase order by id, raising `NotFoundError` if it doesn't exist."""
        purchase_order = self.get_purchase_order(purchase_order_id)
        if purchase_order is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id}",
            )

        return purchase_order

    def list_purchase_orders(
        self,
        order_status: str | None = None,
        purchase_order_ids: list[UUID] | None = None,
    ) -> list[dict]:
        """List purchase orders, narrowed by `order_status` and `purchase_order_ids` (AND'd)."""
        stmt = select(PurchaseOrder)
        if order_status:
            stmt = stmt.where(PurchaseOrder.order_status == order_status)
        if purchase_order_ids:
            stmt = stmt.where(PurchaseOrder.id.in_(purchase_order_ids))

        rows = self._session.scalars(stmt).all()
        return [_purchase_order_to_dict(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        """Purchase-order counts keyed by `order_status`, for diagnostics."""
        rows = self._session.execute(
            select(PurchaseOrder.order_status, func.count()).group_by(PurchaseOrder.order_status)
        ).all()
        return {status: count for status, count in rows}

    def set_order_status(self, purchase_order_id: UUID, order_status: str) -> None:
        """Set a purchase order's `order_status`, raising `NotFoundError` if it doesn't exist."""
        row = self._get_row(purchase_order_id)
        if row is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id}",
            )

        row.order_status = order_status
        self._session.flush()

    def update_current_dates(
        self,
        purchase_order_id: UUID,
        current_delivery_date: date,
        current_required_ship_date: date,
    ) -> None:
        """Apply an accepted or countered date change to the PO's effective dates.

        `PoDeliveryChangeRequestService` is the only intended caller: both columns are
        otherwise immutable after PO creation.
        """
        row = self._get_row(purchase_order_id)
        if row is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id}",
            )

        row.current_delivery_date = current_delivery_date
        row.current_required_ship_date = current_required_ship_date
        self._session.flush()

    def update_negotiation_status(self, purchase_order_id: UUID, negotiation_status: str) -> None:
        """Set `negotiation_status`; `PoDeliveryChangeRequestService` is its only writer."""
        row = self._get_row(purchase_order_id)
        if row is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id}",
            )

        row.negotiation_status = negotiation_status
        self._session.flush()

    # ------------------------------------------------------------------
    # Lines
    # ------------------------------------------------------------------

    def add_line(self, purchase_order_id: UUID, line_number: str, ordered_quantity: float, **fields) -> dict:
        """Create a purchase order line under an existing purchase order."""
        row = PurchaseOrderLine(
            purchase_order_id=purchase_order_id,
            line_number=line_number,
            ordered_quantity=ordered_quantity,
            **fields,
        )
        self._session.add(row)
        self._session.flush()
        return _purchase_order_line_to_dict(row)

    def get_line(self, purchase_order_line_id: UUID) -> dict | None:
        """Fetch a purchase order line by id, or None if not found."""
        row = self._get_line_row(purchase_order_line_id)
        return _purchase_order_line_to_dict(row) if row is not None else None

    def update_line_status(self, purchase_order_line_id: UUID, line_status: str) -> None:
        """Set a PO line's `line_status`, raising `NotFoundError` if the line is unknown."""
        row = self._get_line_row(purchase_order_line_id)
        if row is None:
            raise NotFoundError(
                code="PO_LINE_NOT_FOUND",
                message=f"No purchase order line found with purchase_order_line_id={purchase_order_line_id}",
            )

        row.line_status = line_status
        self._session.flush()

    def list_lines(self, purchase_order_id: UUID) -> list[dict]:
        """List all lines for a purchase order, ordered by line number."""
        rows = self._session.scalars(
            select(PurchaseOrderLine)
            .where(PurchaseOrderLine.purchase_order_id == purchase_order_id)
            .order_by(PurchaseOrderLine.line_number.asc())
        ).all()
        return [_purchase_order_line_to_dict(r) for r in rows]

    def list_lines_by_status(
        self,
        line_status: str | Sequence[str] | None,
        *,
        purchase_order_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """List `purchase_order_line` rows across POs by status, newest first.

        `line_status` takes a single value or a sequence; `None` lists every status.
        `purchase_order_id` narrows the listing to one PO and combines with
        `line_status` rather than replacing it. Pagination is keyed on `updated_at`,
        the same cursor convention as `WorkflowThreadRepository.list_threads`.
        """
        stmt = select(PurchaseOrderLine)
        if purchase_order_id is not None:
            stmt = stmt.where(PurchaseOrderLine.purchase_order_id == purchase_order_id)
        if isinstance(line_status, str):
            stmt = stmt.where(PurchaseOrderLine.line_status == line_status)
        elif line_status is not None:
            stmt = stmt.where(PurchaseOrderLine.line_status.in_(line_status))
        if cursor is not None:
            stmt = stmt.where(PurchaseOrderLine.updated_at < parse_cursor(cursor))
        stmt = stmt.order_by(PurchaseOrderLine.updated_at.desc(), PurchaseOrderLine.id.desc()).limit(limit)

        rows = self._session.scalars(stmt).all()
        items = [_purchase_order_line_to_dict(r) for r in rows]
        next_cursor = next_cursor_from_page(items, limit)
        return items, next_cursor

    def list_open_orders_for_material_plant(
        self,
        material_id: UUID,
        plant_id: UUID,
        exclude_purchase_order_id: UUID,
    ) -> list[UUID]:
        """Return other OPEN purchase orders drawing on the same material and plant.

        Keyed on (material_id, plant_id) to match `production_schedule`, so the result
        is every order competing for the same production line.
        """
        rows = self._session.scalars(
            select(PurchaseOrderLine.purchase_order_id)
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
            .where(
                PurchaseOrderLine.material_id == material_id,
                PurchaseOrderLine.plant_id == plant_id,
                PurchaseOrder.order_status == "OPEN",
                PurchaseOrderLine.purchase_order_id != exclude_purchase_order_id,
            )
            .distinct()
        ).all()
        return list(rows)

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def truncate_all(self) -> None:
        """Delete both PO tables; every FK-referencing table must be cleared first."""
        self._session.execute(delete(PurchaseOrderLine))
        self._session.execute(delete(PurchaseOrder))
        self._session.flush()
