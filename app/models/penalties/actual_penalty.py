"""Post-delivery penalty recorded against a PO."""

from datetime import date
from uuid import UUID

from sqlalchemy import Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import JSONB_OR_JSON, PENALTIES_SCHEMA, UUID_PK, Base, TimestampMixin, generate_uuid7


class ActualPenalty(Base, TimestampMixin):
    """Post-delivery penalty recorded against a purchase order.

    Tracks invoiced or deducted penalties with dispute status. Immutable after insert.
    """

    __tablename__ = "actual_penalty"
    __table_args__ = ({"schema": PENALTIES_SCHEMA},)

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=generate_uuid7)
    actual_penalty_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    purchase_order_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey("purchase_order.id"))
    violation_type: Mapped[str] = mapped_column(String(50))
    actual_penalty_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    invoice_or_deduction_date: Mapped[date] = mapped_column(Date)
    dispute_status: Mapped[str] = mapped_column(String(50), default="NONE")  # NONE/DISPUTED/WAIVED/UPHELD
    claim_facts: Mapped[dict | None] = mapped_column(JSONB_OR_JSON, nullable=True)
