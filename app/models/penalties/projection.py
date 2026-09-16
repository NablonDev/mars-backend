"""Periodic penalty projection for a PO, rule, and projection date."""

from datetime import date
from uuid import UUID

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PENALTIES_SCHEMA, UUID_PK, Base, TimestampMixin, generate_uuid7


class PenaltyProjection(Base, TimestampMixin):
    """Projected penalty row before any mitigation.

    Stores daily penalty facts during scenario replay. Key fields: order_id,
    violation_date, violation_type, projected_qty, unit_cost. Immutable after insert.
    """

    __tablename__ = "penalty_projection"
    __table_args__ = (
        UniqueConstraint(
            "purchase_order_id", "rule_id", "projection_date", name="uq_penalty_projection_po_rule_date"
        ),
        {"schema": PENALTIES_SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=generate_uuid7)
    purchase_order_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey("purchase_order.id"))
    rule_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey(f"{PENALTIES_SCHEMA}.penalty_rule.id"))
    projection_date: Mapped[date] = mapped_column(Date)
    violation_type: Mapped[str] = mapped_column(String(50))
    failure_probability: Mapped[float] = mapped_column(Numeric(5, 4))
    penalty_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    expected_penalty_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    days_to_delivery: Mapped[int] = mapped_column(Integer)
    projection_status: Mapped[str] = mapped_column(String(30), default="OPEN")
    skip_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
