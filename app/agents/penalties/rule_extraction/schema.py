"""Pydantic v2 response models for the three penalty rule extraction LLM stages.

Every constrained field is a `Literal` built from a `vocabulary` tuple, never a
hand-copied list, so a vocabulary change cannot silently desync the schema from what the
prompts promise and the database enforces. Validators fail closed: bad output either
satisfies the model or raises, it is never silently repaired into something that only
looks valid.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.services.penalties.rule_extraction.vocabulary import (
    APPLIES_PER,
    ATTRIBUTE_ROLES,
    BASIS_TYPES,
    CALC_TYPES,
    CALENDAR_UNITS,
    CAP_SCOPES,
    COMBINATORS,
    CONSEQUENCE_TYPES,
    ECONOMIC_EFFECT_TYPES,
    EVENT_ANCHORS,
    EXTERNAL_REFERENCE_TYPES,
    METRIC_CODES,
    METRIC_DENOMINATORS,
    OPERATORS,
    PARTY_SIDE_ROLES,
    PENALTY_CATEGORIES,
    RATIO_METRIC_CODES,
    RESOLUTION_DIFFICULTIES,
    ROUNDING_CONVENTIONS,
    SEGMENT_KEYS,
    SETTLEMENT_METHODS,
    TAX_TREATMENTS,
    TIER_APPLICATIONS,
    TRIGGER_LOGICS,
    VALUE_STATUSES,
    VALUE_UNITS,
    WINDOW_TYPES,
)

# Mirrors the `ck_attribute_currency_required` check on
# `penalties.extracted_penalty_rule_attribute`: these are the VALUE_UNITS entries that
# name a currency rather than a percentage, a time unit, or a count.
_CURRENCY_VALUE_UNITS = ("USD", "EUR", "GBP", "OTHER_CURRENCY")


class CandidateClause(BaseModel):
    """One excerpt the screening stage judged likely to be a penalty clause."""

    section_title: str = Field(..., description="Heading breadcrumb the excerpt was found under, verbatim.")
    excerpt: str = Field(..., description="Verbatim clause text, copied exactly from the source.")
    reason: str = Field(..., description="One sentence naming the trigger, then the consequence.")


class CandidateClauseList(BaseModel):
    """Every candidate clause found in one screening unit, in document order."""

    clauses: list[CandidateClause] = Field(default_factory=list)


class PenaltyRuleExtraction(BaseModel):
    """One classified penalty rule, matching `penalties.extracted_penalty_rule`.

    `penalty_category`, `calc_type`, the two PO flags, `confidence` and `review_notes`
    persist as their own columns. `economic_effect_type`, `consequence_type`,
    `tax_treatment`, `settlement_method`, `obligor_role` and `beneficiary_role` persist
    into that row's `extra` column: real classification a reviewer reads, never input
    the publisher prices from.
    """

    is_penalty_rule: bool = Field(
        ...,
        description=(
            "False only when the excerpt states no consequence of any kind. A structured "
            "decision the pipeline reads, not a note left for a human."
        ),
    )
    penalty_category: Literal[*PENALTY_CATEGORIES] = Field(  # type: ignore[valid-type]
        ..., description="Governed category. Use UNMAPPED only if genuinely nothing else fits."
    )
    calc_type: Literal[*CALC_TYPES] = Field(..., description="Shape of the calculation.")  # type: ignore[valid-type]
    po_shortage_flag: bool = Field(
        False, description="True only if the trigger is a quantity or fulfillment shortfall."
    )
    po_delay_flag: bool = Field(
        False, description="True if the trigger is a timing non-conformance, early delivery included."
    )
    economic_effect_type: Literal[*ECONOMIC_EFFECT_TYPES] = Field(  # type: ignore[valid-type]
        ..., description="Why this amount exists, chosen by mechanism, not by subject matter."
    )
    consequence_type: Literal[*CONSEQUENCE_TYPES] | None = Field(  # type: ignore[valid-type]
        None, description="Required when calc_type is NON_MONETARY."
    )
    tax_treatment: Literal[*TAX_TREATMENTS] | None = None  # type: ignore[valid-type]
    settlement_method: Literal[*SETTLEMENT_METHODS] | None = Field(  # type: ignore[valid-type]
        None, description="How this rule's remedy is collected, only if the clause states a mechanism."
    )
    obligor_role: Literal[*PARTY_SIDE_ROLES] | None = Field(  # type: ignore[valid-type]
        None, description="Which party owes it. RETAILER is the purchasing side, not a retail chain."
    )
    beneficiary_role: Literal[*PARTY_SIDE_ROLES] | None = None  # type: ignore[valid-type]
    confidence: float = Field(..., ge=0, le=1)
    review_notes: str | None = Field(None, description="Required whenever a governed escape value is used.")

    @model_validator(mode="after")
    def _check_required_review_notes(self) -> PenaltyRuleExtraction:
        """Reject a classification that used a governed escape value without explaining why.

        A missing explanation fails the response instead of being filled in with a
        placeholder: a placeholder note reads as a real one to a reviewer skimming a
        queue.
        """
        if not self.is_penalty_rule and not self.review_notes:
            raise ValueError("review_notes is required when is_penalty_rule is false.")
        if self.penalty_category == "UNMAPPED" and not self.review_notes:
            raise ValueError("review_notes is required when penalty_category is UNMAPPED.")
        if self.calc_type == "NON_MONETARY" and self.consequence_type is None:
            raise ValueError("consequence_type is required when calc_type is NON_MONETARY.")
        if self.po_shortage_flag and self.po_delay_flag and not self.review_notes:
            raise ValueError("review_notes is required when both PO flags are set.")
        return self


class PenaltyFact(BaseModel):
    """One extracted fact a penalty rule depends on, matching
    `penalties.extracted_penalty_rule_attribute`.

    The compiler prices a rule off the typed columns alone. Everything else the model
    notices about the clause (window type, event anchor, rounding convention, trigger
    logic, combinator, segment key and value, external reference type, resolution
    difficulty, tier bound inclusivity) belongs in `extra`: preserved, not yet consumed.
    """

    branch_no: int = Field(
        0, ge=0, description="0 is rule-wide, 1 and up is one branch of a tier ladder or conditional."
    )
    attribute_role: Literal[*ATTRIBUTE_ROLES] = Field(  # type: ignore[valid-type]
        ..., description="What kind of fact this row carries: threshold, rate, cap, and so on."
    )
    metric_code: Literal[*METRIC_CODES] | None = None  # type: ignore[valid-type]
    metric_denominator: Literal[*METRIC_DENOMINATORS] | None = Field(  # type: ignore[valid-type]
        None,
        description="What population a ratio metric is measured against. Required for a ratio metric_code.",
    )
    operator: Literal[*OPERATORS] | None = None  # type: ignore[valid-type]
    value: float | None = Field(None, description="Percentages are whole numbers: 4% is value=4, never 0.04.")
    value_max: float | None = Field(None, description="Required alongside value when operator is BETWEEN.")
    value_unit: Literal[*VALUE_UNITS] | None = None  # type: ignore[valid-type]
    value_status: Literal[*VALUE_STATUSES] = Field(  # type: ignore[valid-type]
        ..., description="Why value is or is not populated."
    )
    currency_code: str | None = Field(
        None, min_length=3, max_length=3, description="Required when value_unit names a currency."
    )
    basis_type: Literal[*BASIS_TYPES] | None = Field(  # type: ignore[valid-type]
        None,
        description=(
            "Required when attribute_role is RATE or CAP. NONE means nothing is multiplied "
            "against anything, never leave it null for a flat fee."
        ),
    )
    applies_per: Literal[*APPLIES_PER] | None = None  # type: ignore[valid-type]
    tier_application: Literal[*TIER_APPLICATIONS] | None = Field(  # type: ignore[valid-type]
        None, description="Required whenever a RATE row shares a branch with a THRESHOLD row."
    )
    cap_scope: Literal[*CAP_SCOPES] | None = Field(  # type: ignore[valid-type]
        None, description="Required when attribute_role is CAP or FLOOR."
    )
    source_text: str = Field(..., description="Exact source fragment behind this specific value.")
    confidence: float = Field(..., ge=0, le=1)

    # The long tail. Stored in the `extra` jsonb column rather than typed columns because
    # nothing prices off them yet. Declared field by field rather than as an open dict:
    # structured output requires `additionalProperties: false` on every object, so a
    # free-form dict makes the whole request schema invalid.
    window_type: Literal[*WINDOW_TYPES] | None = None  # type: ignore[valid-type]
    window_length: int | None = None
    window_length_unit: Literal[*CALENDAR_UNITS] | None = None  # type: ignore[valid-type]
    event_anchor: Literal[*EVENT_ANCHORS] | None = None  # type: ignore[valid-type]
    rounding_convention: Literal[*ROUNDING_CONVENTIONS] | None = Field(  # type: ignore[valid-type]
        None, description="Required when a RATE accrues per fractional DAY, WEEK, MONTH, QUARTER or YEAR."
    )
    trigger_logic: Literal[*TRIGGER_LOGICS] | None = None  # type: ignore[valid-type]
    combinator: Literal[*COMBINATORS] | None = None  # type: ignore[valid-type]
    segment_key: Literal[*SEGMENT_KEYS] | None = None  # type: ignore[valid-type]
    segment_value: str | None = None
    is_dynamic_reference: bool = False
    external_reference_type: Literal[*EXTERNAL_REFERENCE_TYPES] | None = None  # type: ignore[valid-type]
    resolution_difficulty: Literal[*RESOLUTION_DIFFICULTIES] | None = None  # type: ignore[valid-type]
    lower_bound_inclusive: bool | None = None
    upper_bound_inclusive: bool | None = None

    @model_validator(mode="after")
    def _check_hard_requirements(self) -> PenaltyFact:
        """Enforce the same conditions as the table's `ck_*` constraints, at parse time.

        Failing here surfaces a malformed row before an insert would, rather than
        after, and before it silently loses a rule's rate or cap.
        """
        if self.attribute_role in ("RATE", "CAP") and self.basis_type is None:
            raise ValueError(f"basis_type is required when attribute_role={self.attribute_role}.")
        if self.attribute_role in ("CAP", "FLOOR") and self.cap_scope is None:
            raise ValueError(f"cap_scope is required when attribute_role={self.attribute_role}.")
        if self.operator == "BETWEEN" and self.value_max is None:
            raise ValueError("value_max is required when operator is BETWEEN.")
        if self.metric_code in RATIO_METRIC_CODES and self.metric_denominator is None:
            raise ValueError(f"metric_denominator is required when metric_code={self.metric_code}.")
        if self.value_unit in _CURRENCY_VALUE_UNITS and self.currency_code is None:
            raise ValueError(f"currency_code is required when value_unit={self.value_unit}.")
        if self.value_status == "PRESENT" and self.value is None and self.value_max is None:
            raise ValueError("value or value_max is required when value_status is PRESENT.")
        return self


class PenaltyFactList(BaseModel):
    """All facts extracted for one already-classified penalty rule."""

    facts: list[PenaltyFact] = Field(default_factory=list)
