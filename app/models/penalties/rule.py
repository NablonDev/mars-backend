"""Penalty rules and their optional tiered bands."""

from datetime import date
from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import PENALTIES_SCHEMA, UUID_PK, Base, TimestampMixin, generate_uuid7

# Every `violation_type` any code path writes today: the engine-recognized shapes
# (SHORT_SHIP/FILL_RATE/OTIF_LATE/ASN_LATE, see `app.services.penalties.projection.types`)
# plus every value `PenaltyRulePublisher`'s `_VIOLATION_TYPE_BY_CATEGORY`
# (`app.services.penalties.rule_extraction.publisher`) can produce. Keep the two in sync by
# hand; this table has no FK to a governed-vocabulary table to enforce it automatically.
_VIOLATION_TYPES = (
    "SHORT_SHIP",
    "FILL_RATE",
    "OTIF_LATE",
    "ASN_LATE",
    "DELIVERY_WINDOW_VIOLATION",
    "DELIVERY_ACCEPTANCE_COST_SHIFT",
    "QUALITY_DEFECT",
    "COVER_PURCHASE",
    "VOLUME_SHORTFALL",
    "OVERAGE_CHARGEBACK",
    "OVERAGE_NONPAYMENT",
    "STORAGE_DURATION",
    "LIABILITY_CAP",
    "FINANCIAL_ADJUSTMENT",
)
# Mirrors `app.services.penalties.rule_extraction.vocabulary.ENGINE_FAMILIES`. Nullable is
# allowed: a rule written before Phase 1 (or seeded directly with no family) carries NULL.
_ENGINE_FAMILIES = (
    "SHORTAGE",
    "DELAY",
    "VOLUME_COMMITMENT",
    "QUALITY",
    "COVER_PURCHASE",
    "FINANCIAL",
    "NON_MONETARY",
    "LIABILITY_CAP",
    "OVERAGE_CHARGEBACK",
    "OVERAGE_NONPAYMENT",
    "STORAGE_DURATION_FEE",
    "UNPRICEABLE",
)


class PenaltyRule(Base, TimestampMixin):
    """Penalty rule defining charge calculation for a retailer and violation type.

    Supports multiple calc types: PER_UNIT, PERCENT_OF_PO, FLAT_FEE, or TIERED.
    Immutable after insert; lifetime controlled by effective_start/end dates.
    """

    __tablename__ = "penalty_rule"
    __table_args__ = (
        CheckConstraint(
            "violation_type IN (" + ", ".join(f"'{v}'" for v in _VIOLATION_TYPES) + ")",
            name="ck_penalty_rule_violation_type",
        ),
        CheckConstraint(
            "engine_family IS NULL OR engine_family IN ("
            + ", ".join(f"'{v}'" for v in _ENGINE_FAMILIES)
            + ")",
            name="ck_penalty_rule_engine_family",
        ),
        {"schema": PENALTIES_SCHEMA},
    )

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
    engine_family: Mapped[str | None] = mapped_column(String(30), nullable=True)
    penalty_category: Mapped[str] = mapped_column(String(60))
    metric_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    metric_denominator: Mapped[str | None] = mapped_column(String(30), nullable=True)
    retailer_agreement_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey("retailer_agreement.id"))
    extracted_rule_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey(f"{PENALTIES_SCHEMA}.extracted_penalty_rule.id"), nullable=True
    )
    measurement_window_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    measurement_window_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    measurement_window_unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rounding_convention: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_engine_priceable: Mapped[bool] = mapped_column(Boolean, default=True)
    commitment_quantity: Mapped[float | None] = mapped_column(Numeric(14, 3), nullable=True)
    commitment_value: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)


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
    band_min: Mapped[float] = mapped_column(Numeric(12, 4))
    band_max: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    rate: Mapped[float] = mapped_column(Numeric(10, 4))
    tier_application: Mapped[str] = mapped_column(String(20), default="CLIFF")
    tier_basis: Mapped[str] = mapped_column(String(30))
