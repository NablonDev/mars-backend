"""Repository for the purchase-order header/line pair (`common.purchase_order`,
`common.purchase_order_line`). Was `app/repositories/order.py`; header/line
kept split per the ERP redesign (see app/models/common/purchase_order.py) --
a confirmation or delivery can partially cover a multi-line PO.

Raises the collapsed `NotFoundError(code="PO_NOT_FOUND")` /
`ConflictError(code="PO_ALREADY_EXISTS")` from `app.core.exceptions`
(Phase 6 collapse) -- the message is passed the purchase order's business
number (`purchase_order_number`) or, for id-keyed lookups where no number is
available, its stringified surrogate id.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.models import PurchaseOrder, PurchaseOrderLine


def describe_no_open_orders(counts: dict[str, int]) -> str | None:
    """Explain a zero-OPEN-purchase-order batch, or None when there is
    nothing to explain (no purchase orders at all, or some are OPEN after
    all).

    A batch that enqueues nothing because every PO is DELIVERED looks
    identical to a broken one in the logs, so both callers say which it is.
    """
    if counts.get("OPEN") or not counts:
        return None

    breakdown = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
    return (
        f"No OPEN purchase orders to enqueue, but {sum(counts.values())} purchase order(s) exist "
        f"({breakdown}). Nothing will run until a purchase order is OPEN -- re-seed, or reopen the "
        "existing purchase orders."
    )


def _purchase_order_to_dict(row: PurchaseOrder) -> dict:
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
        # Raw column values, possibly None -- callers that need the
        # *effective* date (falling back to requested_delivery_date/
        # required_ship_date) should COALESCE explicitly.
        "current_delivery_date": row.current_delivery_date,
        "current_required_ship_date": row.current_required_ship_date,
        "negotiation_status": row.negotiation_status,
    }


def _purchase_order_line_to_dict(row: PurchaseOrderLine) -> dict:
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
        # Deliberate Phase 3 fix (flagged in the phase report): omitted here
        # despite being a real, required column the model comment explains
        # was added back specifically so the projection engine's
        # PERCENT_OF_PO/TIERED calc types can read it -- ProjectionService.
        # build_snapshot's line aggregation needs it.
        "unit_price": float(row.unit_price),
        "uom": row.uom,
        "requested_delivery_date": row.requested_delivery_date,
        "required_ship_date": row.required_ship_date,
        "line_status": row.line_status,
        "raw_payload": row.raw_payload,
    }


class PurchaseOrderRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _is_postgres(self) -> bool:
        return self._session.bind is not None and self._session.bind.dialect.name == "postgresql"

    def _get_row(self, purchase_order_id: UUID) -> PurchaseOrder | None:
        return self._session.get(PurchaseOrder, purchase_order_id)

    def _get_row_by_number(self, purchase_order_number: str) -> PurchaseOrder | None:
        return self._session.scalars(
            select(PurchaseOrder).where(PurchaseOrder.purchase_order_number == purchase_order_number)
        ).first()

    def _get_line_row(self, purchase_order_line_id: UUID) -> PurchaseOrderLine | None:
        return self._session.get(PurchaseOrderLine, purchase_order_line_id)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def create_purchase_order(self, purchase_order_number: str, **fields) -> dict:
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
        row = self._get_row(purchase_order_id)
        return _purchase_order_to_dict(row) if row is not None else None

    def get_by_number(self, purchase_order_number: str) -> dict | None:
        row = self._get_row_by_number(purchase_order_number)
        return _purchase_order_to_dict(row) if row is not None else None

    def get_or_create_purchase_order(self, purchase_order_number: str, **fields) -> dict:
        """Atomic get-or-create for `common.purchase_order`, keyed on
        `purchase_order_number` (`ix_purchase_order_purchase_order_number`,
        unique) -- for PO-validation ingestion only
        (`PoValidationService._ingest_one_line`).

        Deliberately separate from `create_purchase_order`: that method
        keeps its existing hard-create contract unchanged (raises
        `ConflictError(PO_ALREADY_EXISTS)` for a caller that wants to fail
        on a duplicate) -- this method never raises for the normal
        concurrent-ingestion case, resolving to whichever row exists
        (already there, or the winner of a concurrent create) either way.
        Uses `INSERT ... ON CONFLICT (purchase_order_number) DO NOTHING` +
        re-SELECT, same precedent as
        `MasterDataRepository.get_or_create_retailer`/`get_or_create_plant`.
        On non-Postgres (SQLite unit tests), falls back to a plain
        check-then-insert -- safe only single-threaded, same caveat as
        those methods; the real concurrency guarantee is the Postgres
        unique index.
        """
        if self._is_postgres():
            stmt = pg_insert(PurchaseOrder).values(purchase_order_number=purchase_order_number, **fields)
            stmt = stmt.on_conflict_do_nothing(index_elements=["purchase_order_number"])
            self._session.execute(stmt)
            self._session.flush()
            return _purchase_order_to_dict(self._get_row_by_number(purchase_order_number))

        existing = self.get_by_number(purchase_order_number)
        if existing is not None:
            return existing
        row = PurchaseOrder(purchase_order_number=purchase_order_number, **fields)
        self._session.add(row)
        self._session.flush()
        return _purchase_order_to_dict(row)

    def require_purchase_order(self, purchase_order_id: UUID) -> dict:
        purchase_order = self.get_purchase_order(purchase_order_id)
        if purchase_order is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id!r}",
            )

        return purchase_order

    def list_purchase_orders(self, order_status: str | None = None) -> list[dict]:
        stmt = select(PurchaseOrder)
        if order_status:
            stmt = stmt.where(PurchaseOrder.order_status == order_status)

        rows = self._session.scalars(stmt).all()
        return [_purchase_order_to_dict(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        """Purchase-order counts keyed by `order_status`, for diagnostics."""
        rows = self._session.execute(
            select(PurchaseOrder.order_status, func.count()).group_by(PurchaseOrder.order_status)
        ).all()
        return {status: count for status, count in rows}

    def set_order_status(self, purchase_order_id: UUID, order_status: str) -> None:
        row = self._get_row(purchase_order_id)
        if row is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id!r}",
            )

        row.order_status = order_status
        self._session.flush()

    def update_current_dates(
        self,
        purchase_order_id: UUID,
        current_delivery_date: date,
        current_required_ship_date: date,
    ) -> None:
        """Apply an accepted/countered delivery-date change to the PO's
        effective dates. See PoDeliveryChangeRequestService --
        the only intended caller, since these two columns are otherwise
        immutable after PO creation."""
        row = self._get_row(purchase_order_id)
        if row is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id!r}",
            )

        row.current_delivery_date = current_delivery_date
        row.current_required_ship_date = current_required_ship_date
        self._session.flush()

    def update_negotiation_status(self, purchase_order_id: UUID, negotiation_status: str) -> None:
        """Set `PurchaseOrder.negotiation_status`. SINGLE WRITER: only
        `PoDeliveryChangeRequestService` may call this -- see the
        column comment on `PurchaseOrder.negotiation_status` for the full
        rule."""
        row = self._get_row(purchase_order_id)
        if row is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id!r}",
            )

        row.negotiation_status = negotiation_status
        self._session.flush()

    # ------------------------------------------------------------------
    # Lines
    # ------------------------------------------------------------------

    def add_line(self, purchase_order_id: UUID, line_number: str, ordered_quantity: float, **fields) -> dict:
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
        row = self._get_line_row(purchase_order_line_id)
        return _purchase_order_line_to_dict(row) if row is not None else None

    def get_or_create_line(
        self, purchase_order_id: UUID, line_number: str, ordered_quantity: float, **fields
    ) -> dict:
        """Atomic get-or-create for `common.purchase_order_line`, keyed on
        `(purchase_order_id, line_number)` (`uq_purchase_order_line_po_line_number`,
        unique) -- for PO-validation ingestion only.

        First-write-wins/idempotent by design: a duplicate submission of the
        same `(purchase_order_id, line_number)` -- whether sequential or a
        genuine concurrent race -- returns the EXISTING row unchanged;
        `ordered_quantity`/`**fields` are only used the first time this pair
        is seen and are never used to overwrite already-stored line data on
        a later call. Distinct from `add_line` (unconditional insert),
        which is unchanged. Same Postgres `ON CONFLICT ... DO NOTHING` +
        re-SELECT / SQLite check-then-insert-fallback shape as the other
        three `get_or_create_*` methods.
        """
        if self._is_postgres():
            stmt = pg_insert(PurchaseOrderLine).values(
                purchase_order_id=purchase_order_id,
                line_number=line_number,
                ordered_quantity=ordered_quantity,
                **fields,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["purchase_order_id", "line_number"])
            self._session.execute(stmt)
            self._session.flush()
            row = self._session.scalars(
                select(PurchaseOrderLine).where(
                    PurchaseOrderLine.purchase_order_id == purchase_order_id,
                    PurchaseOrderLine.line_number == line_number,
                )
            ).one()
            return _purchase_order_line_to_dict(row)

        existing = self._session.scalars(
            select(PurchaseOrderLine).where(
                PurchaseOrderLine.purchase_order_id == purchase_order_id,
                PurchaseOrderLine.line_number == line_number,
            )
        ).first()
        if existing is not None:
            return _purchase_order_line_to_dict(existing)
        return self.add_line(purchase_order_id, line_number, ordered_quantity, **fields)

    def update_line_status(self, purchase_order_line_id: UUID, line_status: str) -> None:
        """Deliberate Phase 3 addition (flagged in the phase report):
        deferred by Phase 2, no `purchase_order_line.line_status` writer
        existed at all. `PoValidationService`'s own transitions (e.g.
        AWAITING_DECISION on first interrupt) need one directly -- the old
        pre-restructure service called this same shape
        (`PoLineRepository.update_status`) itself, not a graph node."""
        row = self._get_line_row(purchase_order_line_id)
        if row is None:
            raise NotFoundError(
                code="PO_LINE_NOT_FOUND",
                message=f"No purchase order line found with purchase_order_line_id={purchase_order_line_id!r}",
            )

        row.line_status = line_status
        self._session.flush()

    def list_lines(self, purchase_order_id: UUID) -> list[dict]:
        rows = self._session.scalars(
            select(PurchaseOrderLine)
            .where(PurchaseOrderLine.purchase_order_id == purchase_order_id)
            .order_by(PurchaseOrderLine.line_number.asc())
        ).all()
        return [_purchase_order_line_to_dict(r) for r in rows]

    def list_recent_lines(self, limit: int = 5) -> list[dict]:
        """Most recently created PO lines across all purchase orders, for the
        CMIR Intelligence Module's "PO Audit Trail" panel -- no existing query
        lists lines across POs (list_lines is scoped to one purchase_order_id),
        so this is a new, real, read-only query: no schema change, real
        columns only. `func.coalesce(line.requested_delivery_date,
        po.current_delivery_date, po.requested_delivery_date)` picks the same
        "effective delivery date" precedence _purchase_order_to_dict's own
        current_delivery_date comment documents (falls back when the line
        itself doesn't carry its own date)."""
        rows = self._session.execute(
            select(
                PurchaseOrder.purchase_order_number,
                PurchaseOrderLine.retailer_material_code,
                PurchaseOrderLine.ordered_quantity,
                PurchaseOrderLine.line_status,
                func.coalesce(
                    PurchaseOrderLine.requested_delivery_date,
                    PurchaseOrder.current_delivery_date,
                    PurchaseOrder.requested_delivery_date,
                ).label("delivery_date"),
            )
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
            .order_by(PurchaseOrderLine.created_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "purchase_order_number": r.purchase_order_number,
                "retailer_material_code": r.retailer_material_code,
                "ordered_quantity": float(r.ordered_quantity),
                "line_status": r.line_status,
                "delivery_date": r.delivery_date,
            }
            for r in rows
        ]

    def list_open_orders_for_material_plant(
        self,
        material_id: UUID,
        plant_id: UUID,
        exclude_purchase_order_id: UUID,
    ) -> list[UUID]:
        """Other OPEN purchase orders whose line draws on the same
        material/plant (production line). Mirrors the old
        list_open_orders_for_sku_location, re-keyed on
        (material_id, plant_id) -- production_schedule's own key -- rather
        than (sku_id, location_id)."""
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
        """Deletes every purchase_order_line row, then every purchase_order
        row. Caller must first clear every other table that FK-references
        either (order_confirmation*, delivery*, shipment, production_*,
        demand_exception -- see FulfillmentRepository.truncate_all --
        plus mitigation_input/mitigation_option and the penalties tables)
        in FK-safe order before calling this."""
        self._session.execute(delete(PurchaseOrderLine))
        self._session.execute(delete(PurchaseOrder))
        self._session.flush()
