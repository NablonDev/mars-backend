"""Enums, errors, and data structures for the penalty-dispute engine.

Framework-free, matching `app.services.penalties.projection.types`: no
SQLAlchemy or FastAPI imports here or in `engine.py`. `app.core.exceptions`
registers its FastAPI handlers in the same module as its exception classes, so
the two error types below subclass plain `ValueError` instead;
`DisputeResolutionService.analyze` is the only place they become the
`BusinessRuleError(code=...)` an API caller sees.

`DisputeFacts` is deliberately not `OrderSnapshot`. The projection snapshot
mixes real facts with probability-driving inputs (production status,
appointment status, carrier reliability) that a post-delivery dispute has no
use for. `DisputeFacts` carries only the final facts the pricing functions
need, plus `grace_period_days`, which `PenaltyRule`'s frozen contract excludes.

`delivered_qty` comes from actual delivery (`common.delivery`,
`delivery_line`), never from `order_confirmation`: that table holds the
retailer's pre-delivery promise, not what actually shipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class DisputeVerdict(Enum):
    """Outcome of comparing the claimed penalty amount against the computed one."""

    NO_PAY = "NO_PAY"
    PAY_PARTIAL = "PAY_PARTIAL"
    PAY_FULL = "PAY_FULL"


class InsufficientDataForDisputeError(ValueError):
    """A fact the violation family needs was never recorded as of the charge date.

    See `FAMILY_REQUIRED_KEYS` below for which fact(s) each `engine_family` needs.
    Missing must never collapse into "confirmed zero" or "confirmed on time",
    which would let the engine refuse or grant a charge on absence of data.
    `DisputeResolutionService.analyze` re-raises this as
    `BusinessRuleError(code="INSUFFICIENT_DATA_FOR_DISPUTE")`, leaving the
    dispute OPEN.
    """


class UnsupportedDisputeCalcError(ValueError):
    """The rule's `engine_family`/`calc_type` combination has no dispute recompute path.

    Raised for a family with no dispatch-table entry in
    `app.services.penalties.dispute.engine`, or a `calc_type` its measure function doesn't
    branch on. `DisputeResolutionService.analyze` re-raises this as
    `BusinessRuleError(code="DISPUTE_CALC_NOT_SUPPORTED")`, leaving the dispute OPEN: this
    backlog grows as more families are admitted upstream, a known operational concern this
    phase does not solve.
    """


@dataclass
class DisputeFacts:
    """Real, final post-delivery facts as of the historical charge date.

    Never the probability-driven estimates `app.services.penalties.projection`
    works from. Fields below the DELAY block are Phase 4's family-specific facts
    (docs/architecture/extraction-engine-integration-plan.md section 10): each is either
    claim-supplied (populated from `actual_penalty.claim_facts`, the retailer's own
    assertion) or Mars-derived (populated by `DisputeResolutionService.analyze` from a
    repository or the matched rule, never from claim_facts). `FAMILY_REQUIRED_KEYS` below
    is the authoritative map of which is which per `engine_family`.
    """

    order_qty: int
    unit_price: float
    # Real, final delivered quantity as of the charge date. None means no
    # delivery fact is on record at that date, never "confirmed zero" (see
    # InsufficientDataForDisputeError). SHORTAGE-family disputes only.
    delivered_qty: float | None
    required_delivery_date: date
    # None if no delivery/shipment fact is on record yet. DELAY-family
    # disputes only.
    actual_delivery_date: date | None
    grace_period_days: int = 0
    # QUALITY-family claim-supplied fact: a defect count the retailer's DC observed, which
    # Mars holds nowhere else.
    defect_units: float | None = None
    # QUALITY-family claim-supplied fact: a defect rate, used instead of defect_units for a
    # PERCENT_OF_PO rule.
    defect_rate_pct: float | None = None
    # COVER_PURCHASE-family claim-supplied fact: what the retailer actually paid a
    # substitute vendor. Mars has no record of this.
    replacement_cost_paid: float | None = None
    # COVER_PURCHASE-family Mars-derived fact: order_qty * unit_price. Never read from
    # claim_facts, even when one happens to be present there.
    original_cost: float | None = None
    # FINANCIAL-family claim-supplied fact: an occurrence count Mars does not otherwise track.
    occurrence_count: float | None = None
    # STORAGE_DURATION_FEE-family claim-supplied fact: days the retailer says product sat in
    # its own DC. Mars has no record of this.
    storage_days: float | None = None
    # VOLUME_COMMITMENT-family Mars-derived fact: the matched rule's own commitment_quantity.
    # Never read from claim_facts.
    committed_quantity: float | None = None
    # VOLUME_COMMITMENT-family Mars-derived fact: PurchaseOrderRepository's own purchase-history
    # total. Never read from claim_facts, even when a claim asserts a conflicting total (4f).
    actual_purchase_quantity: float | None = None


@dataclass(frozen=True)
class DisputeFamilyKeys:
    """One `engine_family`'s dispute-recompute fact-authority contract.

    `claim_supplied_keys` may be populated from `actual_penalty.claim_facts`;
    `mars_derived_keys` must always be populated from a repository or the matched rule,
    never from claim_facts, even when claim_facts supplies a conflicting value (decision
    #2, docs/architecture/extraction-engine-integration-plan.md section 10). Both are
    `DisputeFacts` field names; QUALITY carries neither, since its bespoke measure function
    branches between `defect_units`/`defect_rate_pct` by the rule's own `calc_type` rather
    than requiring one fixed key.
    """

    claim_supplied_keys: tuple[str, ...]
    mars_derived_keys: tuple[str, ...]


# Structural encoding of the claim-supplied/Mars-derived boundary, keyed by the same
# `engine_family` values `app.services.penalties.projection.types` and
# `app.services.penalties.rule_extraction.vocabulary.ENGINE_FAMILIES` use.
# `app.services.penalties.dispute.engine._require_family_keys` reads this to raise
# `InsufficientDataForDisputeError` before any pricing math runs.
FAMILY_REQUIRED_KEYS: dict[str, DisputeFamilyKeys] = {
    "SHORTAGE": DisputeFamilyKeys(claim_supplied_keys=(), mars_derived_keys=("delivered_qty",)),
    "DELAY": DisputeFamilyKeys(claim_supplied_keys=(), mars_derived_keys=("actual_delivery_date",)),
    # Empty on both sides: `_price_quality` requires defect_rate_pct or defect_units
    # depending on the rule's own calc_type, not one fixed key.
    "QUALITY": DisputeFamilyKeys(claim_supplied_keys=(), mars_derived_keys=()),
    "COVER_PURCHASE": DisputeFamilyKeys(
        claim_supplied_keys=("replacement_cost_paid",), mars_derived_keys=("original_cost",)
    ),
    "FINANCIAL": DisputeFamilyKeys(claim_supplied_keys=("occurrence_count",), mars_derived_keys=()),
    "STORAGE_DURATION_FEE": DisputeFamilyKeys(claim_supplied_keys=("storage_days",), mars_derived_keys=()),
    "VOLUME_COMMITMENT": DisputeFamilyKeys(
        claim_supplied_keys=(), mars_derived_keys=("committed_quantity", "actual_purchase_quantity")
    ),
}


@dataclass
class DisputeCalculation:
    """Everything `PenaltyDispute.analyzed_at` onward gets persisted from."""

    computed_amount: float
    claimed_amount: float
    delta_amount: float  # claimed_amount - computed_amount
    verdict: DisputeVerdict
    # Audit trail feeding `analysis_breakdown`: violation family, the shortfall
    # or lateness measure actually used, cap info. Never read back by the
    # engine itself.
    calc_trace: dict
