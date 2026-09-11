"""Penalty rules and their optional tiered bands."""

from datetime import date
from uuid import UUID

from sqlalchemy import CHAR, Boolean, Date, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PENALTIES_SCHEMA, UUID_PK, Base, TimestampMixin, generate_uuid7


class PenaltyRule(Base, TimestampMixin):
    """Penalty rule defining charge calculation for a retailer and violation type.

    Supports multiple calc types: PER_UNIT, PERCENT_OF_PO, FLAT_FEE, or TIERED.
    Immutable after insert; lifetime controlled by effective_start/end dates.
    """

    __tablename__ = "penalty_rule"
    __table_args__ = ({"schema": PENALTIES_SCHEMA},)

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=generate_uuid7)
    rule_code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    retailer_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey("retailer.id"))
    violation_type: Mapped[str] = mapped_column(String(30))
    threshold_pct: Mapped[float] = mapped_column(Numeric(6, 4), default=0.0)
    calc_type: Mapped[str] = mapped_column(String(20))  # PER_UNIT / PERCENT_OF_PO / FLAT_FEE / TIERED
    rate: Mapped[float] = mapped_column(Numeric(10, 4), default=0.0)
    cap_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    grace_period_days: Mapped[int] = mapped_column(Integer, default=0)
    effective_start_date: Mapped[date] = mapped_column(Date, default=date(2026, 1, 1))
    effective_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_doc_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    basis_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    applies_per: Mapped[str | None] = mapped_column(String(20), nullable=True)
    currency_code: Mapped[str] = mapped_column(CHAR(3), default="USD")


class PenaltyRuleTier(Base, TimestampMixin):
    """One tier band for a tiered penalty rule.

    Defines a rate applicable to a shortage/delay percentage range.
    Keyed by (rule_id, tier_code); immutable after insert.
    """

    __tablename__ = "penalty_rule_tier"
    __table_args__ = (
        UniqueConstraint("rule_id", "tier_code", name="uq_penalty_rule_tier_rule_code"),
        {"schema": PENALTIES_SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=generate_uuid7)
    rule_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey(f"{PENALTIES_SCHEMA}.penalty_rule.id"))
    tier_code: Mapped[str] = mapped_column(String(50))
    band_min: Mapped[float] = mapped_column(Numeric(6, 4))
    band_max: Mapped[float] = mapped_column(Numeric(6, 4))
    rate: Mapped[float] = mapped_column(Numeric(10, 4))
