"""Extracted penalty clauses, their attributes, and rule publications in the `penalties` schema."""

from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    JSONB_OR_JSON,
    PENALTIES_SCHEMA,
    PROCESS_SCHEMA,
    UUID_PK,
    Base,
    TimestampMixin,
    generate_uuid7,
)


class ExtractedPenaltyRule(Base, TimestampMixin):
    """One penalty clause found by extraction, pending review before publication.

    `ix_extracted_penalty_rule_contract_status` (contract_id, status WHERE deleted_at IS
    NULL) is raw DDL in the migration, not declared here; see
    app/models/process/job.py's JobItem docstring for why a partial index can't be.
    """

    __tablename__ = "extracted_penalty_rule"
    __table_args__ = (
        CheckConstraint(
            "pricing_readiness IN ('READY', 'NEEDS_EXTERNAL_FIGURE', 'AWAITING_DATA', "
            "'UNSUPPORTED_SHAPE', 'NOT_A_CHARGE')",
            name="ck_extracted_penalty_rule_pricing_readiness",
        ),
        CheckConstraint(
            "status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED')",
            name="ck_extracted_penalty_rule_status",
        ),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_extracted_penalty_rule_confidence"),
        UniqueConstraint(
            "agent_run_id", "clause_fingerprint", name="uq_extracted_penalty_rule_run_fingerprint"
        ),
        {"schema": PENALTIES_SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=generate_uuid7)
    contract_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey("retailer_agreement.id"))
    agent_run_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey(f"{PROCESS_SCHEMA}.agent_run.id"))
    section: Mapped[str | None] = mapped_column(String(200), nullable=True)
    clause_text: Mapped[str] = mapped_column(Text)
    clause_fingerprint: Mapped[str] = mapped_column(CHAR(32))
    penalty_category: Mapped[str] = mapped_column(String(60))
    calc_type: Mapped[str] = mapped_column(String(30))
    po_shortage_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    po_delay_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    pricing_readiness: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="PENDING_REVIEW")
    confidence: Mapped[float] = mapped_column(Numeric(3, 2))
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict] = mapped_column(JSONB_OR_JSON, default=dict)


class ExtractedPenaltyRuleAttribute(Base, TimestampMixin):
    """One extracted fact (rate, threshold, cap, or similar) for an extracted rule.

    `branch_no` 0 is rule-wide; 1 and up is one branch of a tier ladder or conditional.
    """

    __tablename__ = "extracted_penalty_rule_attribute"
    __table_args__ = (
        CheckConstraint(
            "attribute_role IN ('THRESHOLD', 'RATE', 'CAP', 'FLOOR', 'GRACE_PERIOD', "
            "'CURE_PERIOD', 'TIME_WINDOW', 'QUANTITY', 'BASIS', 'ESCALATION_FACTOR', "
            "'ROUNDING_RULE', 'EXCLUSION_CONDITION', 'OTHER')",
            name="ck_extracted_penalty_rule_attribute_attribute_role",
        ),
        CheckConstraint(
            "operator IS NULL OR operator IN ('EQ', 'GT', 'GTE', 'LT', 'LTE', 'BETWEEN', 'ALWAYS')",
            name="ck_extracted_penalty_rule_attribute_operator",
        ),
        CheckConstraint(
            "value_status IN ('PRESENT', 'NOT_APPLICABLE', 'NOT_STATED', 'REDACTED', "
            "'EXTERNAL_REFERENCE', 'EXTRACTION_UNCERTAIN')",
            name="ck_extracted_penalty_rule_attribute_value_status",
        ),
        CheckConstraint(
            "tier_application IS NULL OR tier_application IN ('CLIFF', 'MARGINAL', 'NOT_APPLICABLE')",
            name="ck_extracted_penalty_rule_attribute_tier_application",
        ),
        CheckConstraint(
            "cap_scope IS NULL OR cap_scope IN ('RATE_CEILING', 'AMOUNT_CEILING', "
            "'DURATION_CEILING', 'QUANTITY_CEILING')",
            name="ck_extracted_penalty_rule_attribute_cap_scope",
        ),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_extracted_penalty_rule_attribute_confidence"),
        # A percentage with no stated population is three different dollar figures.
        CheckConstraint(
            "metric_code NOT IN ('FILL_RATE_PCT', 'OTIF_PCT', 'SHORTFALL_PCT', "
            "'DAMAGE_RATE_PCT', 'EXPIRED_UNSALABLE_PCT') OR metric_denominator IS NOT NULL",
            name="ck_attribute_denominator_required",
        ),
        CheckConstraint(
            "attribute_role NOT IN ('RATE', 'CAP') OR basis_type IS NOT NULL",
            name="ck_attribute_basis_required",
        ),
        CheckConstraint(
            "value_unit NOT IN ('USD', 'EUR', 'GBP', 'OTHER_CURRENCY') OR currency_code IS NOT NULL",
            name="ck_attribute_currency_required",
        ),
        CheckConstraint(
            "value_status <> 'PRESENT' OR value IS NOT NULL OR value_max IS NOT NULL",
            name="ck_attribute_present_has_value",
        ),
        Index("ix_extracted_penalty_rule_attribute_rule_branch", "extracted_rule_id", "branch_no"),
        {"schema": PENALTIES_SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=generate_uuid7)
    extracted_rule_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey(f"{PENALTIES_SCHEMA}.extracted_penalty_rule.id", ondelete="CASCADE")
    )
    branch_no: Mapped[int] = mapped_column(Integer, default=0)
    attribute_role: Mapped[str] = mapped_column(String(30))
    metric_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    metric_denominator: Mapped[str | None] = mapped_column(String(30), nullable=True)
    operator: Mapped[str | None] = mapped_column(String(10), nullable=True)
    value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_max: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    value_status: Mapped[str] = mapped_column(String(30))
    currency_code: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    basis_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    applies_per: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tier_application: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cap_scope: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Numeric(3, 2))
    extra: Mapped[dict] = mapped_column(JSONB_OR_JSON, default=dict)


class RulePublication(Base, TimestampMixin):
    """Audit row recording why an extracted rule was published or rejected."""

    __tablename__ = "rule_publication"
    __table_args__ = (
        CheckConstraint("outcome IN ('PUBLISHED', 'REJECTED')", name="ck_rule_publication_outcome"),
        {"schema": PENALTIES_SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=generate_uuid7)
    extracted_rule_id: Mapped[UUID] = mapped_column(
        UUID_PK, ForeignKey(f"{PENALTIES_SCHEMA}.extracted_penalty_rule.id")
    )
    penalty_rule_id: Mapped[UUID | None] = mapped_column(
        UUID_PK, ForeignKey(f"{PENALTIES_SCHEMA}.penalty_rule.id"), nullable=True
    )
    agent_run_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey(f"{PROCESS_SCHEMA}.agent_run.id"))
    outcome: Mapped[str] = mapped_column(String(20))
    reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
