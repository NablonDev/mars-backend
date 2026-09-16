"""API schemas for `penalties.penalty_rule`/`penalty_rule_tier`."""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

# Mirrors `ck_penalty_rule_violation_type` (app/models/penalties/rule.py); keep both in sync
# by hand, same as that CHECK constraint's own comment says.
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


class PenaltyRuleTierSchema(BaseModel):
    """One rate band of a `calc_type=TIERED` penalty rule. `band_max=None` means unbounded."""

    band_min: float = Field(ge=0.0, le=1.0)
    band_max: float | None = Field(default=None, ge=0.0, le=1.01)  # 1.01 closes "30%+" as (0.30, 1.01)
    rate: float


class PenaltyRuleRequest(BaseModel):
    """Request body for creating/updating a `penalty_rule`."""

    rule_code: str
    retailer_id: UUID
    retailer_agreement_id: UUID
    penalty_category: str
    violation_type: Literal[*_VIOLATION_TYPES]  # type: ignore[valid-type]
    calc_type: Literal["PER_UNIT", "PERCENT_OF_PO", "FLAT_FEE", "TIERED"]
    rate: float = 0.0
    threshold_pct: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="FRACTION, e.g. 0.02 for 2%; never a whole-number percent.",
    )
    cap_amount: float | None = None
    grace_period_days: int = 0
    effective_start_date: date = date(2026, 1, 1)
    effective_end_date: date | None = None
    source_doc_reference: str | None = None
    tiers: list[PenaltyRuleTierSchema] | None = None

    @model_validator(mode="after")
    def _tiered_requires_tiers(self) -> PenaltyRuleRequest:
        """Enforce that a `calc_type=TIERED` rule supplies at least one tier band."""
        if self.calc_type == "TIERED" and not self.tiers:
            raise ValueError("calc_type=TIERED requires at least one tier band")
        return self


class PenaltyRuleResponse(BaseModel):
    """Response shape for a `penalty_rule` row."""

    id: UUID
    rule_code: str
    retailer_id: UUID
    violation_type: str
    calc_type: str
    rate: float
    threshold_pct: float
    cap_amount: float | None
    is_active: bool
    grace_period_days: int
    effective_start_date: date
    effective_end_date: date | None
    source_doc_reference: str | None
