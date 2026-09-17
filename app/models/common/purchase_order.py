"""Purchase order header and line, split so one confirmation or delivery can partially cover a PO."""

from datetime import date
from uuid import UUID

from sqlalchemy import Date, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import JSONB_OR_JSON, UUID_PK, Base, TimestampMixin, generate_uuid7


class PurchaseOrder(Base, TimestampMixin):
    """Purchase order header (one row per order).

    Tracks order metadata, retailer assignment, and delivery-negotiation state.
    Immutable core fields (order_date, requested_delivery_date); current_delivery_date
    and negotiation_status mutable for delivery change request (DCR) tracking.
    """

    __tablename__ = "purchase_order"
    __table_args__ = (
        Index("ix_purchase_order_retailer_order_date", "retailer_id", "order_date"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=generate_uuid7)
    purchase_order_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    retailer_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey("retailer.id"))
    retailer_po_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    order_date: Mapped[date] = mapped_column(Date)
    requested_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    required_ship_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    order_status: Mapped[str] = mapped_column(String(50), default="OPEN")
    source_system: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_document_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_document_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Kept from the pre-ERP-split `sales_order` model: current per-PO
    # delivery-negotiation state, not in docs/redesigned-schema.md's
    # common.po table (drafted before this feature existed) but real,
    # currently-used data backing PoDeliveryChangeRequest.
    current_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    current_required_ship_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    negotiation_status: Mapped[str] = mapped_column(String(30), default="NONE", index=True)


class PurchaseOrderLine(Base, TimestampMixin):
    """Purchase order line (one or more per purchase order).

    Tracks line-level inventory, pricing, and fulfillment details.
    Keyed by (purchase_order_id, line_number); immutable after insert.
    """

    __tablename__ = "purchase_order_line"
    __table_args__ = (
        UniqueConstraint("purchase_order_id", "line_number", name="uq_purchase_order_line_po_line_number"),
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=generate_uuid7)
    purchase_order_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey("purchase_order.id"))
    line_number: Mapped[str] = mapped_column(String(50))
    retailer_po_line_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sku_id: Mapped[UUID | None] = mapped_column(UUID_PK, ForeignKey("sku.id"), nullable=True)
    material_id: Mapped[UUID | None] = mapped_column(UUID_PK, ForeignKey("material.id"), nullable=True)
    retailer_material_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    plant_id: Mapped[UUID | None] = mapped_column(UUID_PK, ForeignKey("plant.id"), nullable=True)
    storage_location_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("storage_location.id"), nullable=True
    )
    ship_to_location_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey("retailer_location.id"), nullable=True
    )
    ordered_quantity: Mapped[float] = mapped_column(Numeric(18, 3))
    # The projection engine's PERCENT_OF_PO and TIERED calc types derive
    # po_value = ordered_quantity * unit_price. Price sits at line grain, not
    # header grain, because it genuinely varies per line.
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2))
    uom: Mapped[str | None] = mapped_column(String(30), nullable=True)
    requested_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    required_ship_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    line_status: Mapped[str] = mapped_column(String(50), default="OPEN")
    raw_payload: Mapped[dict | None] = mapped_column(JSONB_OR_JSON, nullable=True)
