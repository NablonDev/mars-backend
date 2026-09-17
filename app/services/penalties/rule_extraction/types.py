"""Pure dataclasses for the extraction-to-pricing publication boundary.

`StagedRule`/`StagedFact` mirror the typed columns of `penalties.extracted_penalty_rule`
and `penalties.extracted_penalty_rule_attribute` as the publisher receives them.
`PublishedRule`/`PublishedTier` are its accepted output, shaped for an insert into
`penalties.penalty_rule` and `penalties.penalty_rule_tier`. `RejectedPublication` is its
rejected output, one of the eighteen `RejectionReason` codes below.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any


@dataclass
class StagedFact:
    """One row from `penalties.extracted_penalty_rule_attribute`."""

    branch_no: int
    attribute_role: str
    metric_code: str | None = None
    metric_denominator: str | None = None
    operator: str | None = None
    value: Decimal | None = None
    value_max: Decimal | None = None
    value_unit: str | None = None
    value_status: str = "PRESENT"
    currency_code: str | None = None
    basis_type: str | None = None
    applies_per: str | None = None
    tier_application: str | None = None
    cap_scope: str | None = None
    # The attribute row's own `extra` JSONB: window_type/window_length/window_length_unit,
    # event_anchor, rounding_convention, combinator, segment_key/value, and similar fields
    # with no typed column of their own. Otherwise unreachable by the publisher.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StagedRule:
    """An approved `penalties.extracted_penalty_rule` row plus its facts, as the publisher receives it."""

    id: str
    retailer_agreement_id: str
    clause_fingerprint: str
    penalty_category: str
    calc_type: str
    po_shortage_flag: bool
    po_delay_flag: bool
    pricing_readiness: str
    status: str
    facts: list[StagedFact] = field(default_factory=list)
    # The rule's own `extra` JSONB (classification context kept for a reviewer, not read by
    # the publisher today). Carried through for the same reachability reason as `StagedFact.extra`.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishedTier:
    """One tier band for a published TIERED rule, half-open `[band_min, band_max)`, as fractions.

    `band_max=None` means the top band is unbounded; never `Decimal("Infinity")`.
    """

    tier_code: str
    band_min: Decimal
    band_max: Decimal | None
    rate: Decimal
    tier_application: str
    tier_basis: str


@dataclass
class PublishedRule:
    """Everything needed to insert one `penalties.penalty_rule` row plus its tier rows.

    `retailer_code` passes through rather than resolving to `retailer_id`: the publisher
    is pure and has no session to look up the retailer's surrogate id.
    """

    rule_code: str
    retailer_code: str
    violation_type: str
    calc_type: str
    rate: Decimal
    threshold_pct: Decimal
    cap_amount: Decimal | None
    grace_period_days: int
    basis_type: str | None
    currency_code: str
    applies_per: str | None
    effective_start_date: date
    extracted_rule_id: str
    engine_family: str
    penalty_category: str
    retailer_agreement_id: str
    metric_code: str | None = None
    metric_denominator: str | None = None
    measurement_window_type: str | None = None
    measurement_window_length: int | None = None
    measurement_window_unit: str | None = None
    rounding_convention: str | None = None
    # Snapshot of today's engine capability, not derived from `engine_family` when read back.
    # See the matching warning on `app.models.penalties.rule.PenaltyRule.is_engine_priceable`
    # and on `PenaltyRulePublisher`'s `_ENGINE_PRICEABLE_FAMILIES`: this must be reviewed and
    # potentially updated whenever engine capability changes, since it does not update itself.
    is_engine_priceable: bool = True
    tiers: list[PublishedTier] = field(default_factory=list)


class RejectionReason(str, Enum):
    """The exact `rule_publication.reason_code` values a rejected staged rule can carry."""

    NOT_APPROVED = "NOT_APPROVED"
    NOT_READY = "NOT_READY"
    NOT_PO_SCOPED = "NOT_PO_SCOPED"
    UNSUPPORTED_CALC_TYPE = "UNSUPPORTED_CALC_TYPE"
    UNMAPPED_VIOLATION_TYPE = "UNMAPPED_VIOLATION_TYPE"
    PERCENT_OF_INVOICE = "PERCENT_OF_INVOICE"
    NO_RATE_VALUE = "NO_RATE_VALUE"
    AMBIGUOUS_RATE_BRANCH = "AMBIGUOUS_RATE_BRANCH"
    MARGINAL_TIERS = "MARGINAL_TIERS"
    NON_HALF_OPEN_TIERS = "NON_HALF_OPEN_TIERS"
    NON_AMOUNT_CAP = "NON_AMOUNT_CAP"
    UNSUPPORTED_BASIS = "UNSUPPORTED_BASIS"
    UNSUPPORTED_ACCRUAL = "UNSUPPORTED_ACCRUAL"
    THRESHOLD_OUT_OF_RANGE = "THRESHOLD_OUT_OF_RANGE"
    TIER_BAND_GAP = "TIER_BAND_GAP"
    EXTERNAL_FIGURE = "EXTERNAL_FIGURE"
    MIXED_CURRENCY = "MIXED_CURRENCY"
    ALREADY_PUBLISHED = "ALREADY_PUBLISHED"


@dataclass
class RejectedPublication:
    """Why one staged rule was not published, for a `rule_publication` audit row."""

    reason_code: RejectionReason
    reason_detail: str
