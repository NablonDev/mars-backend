"""Deterministic dispute recompute-and-classify engine.

Reuses the projection engine's pricing functions (`price_shortage_penalty`,
`price_delay_penalty`) unchanged, fed with real post-delivery facts rather than
risk-adjusted estimates. Pricing logic is never reimplemented here; this module
computes only how much of a rule's shortfall or delay measure actually
happened. `shortfall_units_for_pricing` is deliberately never called, because
its `ANTICIPATED_SHORTFALL_PCT` fallback is a pre-delivery risk estimate and is
meaningless once delivery is final.

The LLM never decides pay, no-pay, or how much; this module alone does.
`DisputeResolutionService.analyze` is its only caller, and the dispute-summary agent only
narrates a verdict already computed and persisted here.

`price_delay_penalty` raises `NotImplementedError` for a TIERED delay rule, a
gap inherited from the projection engine, where tiered pricing exists for
shortage rules only. `price_violation` converts that into
`UnsupportedDisputeCalcError`, which `DisputeResolutionService.analyze` turns into a
`BusinessRuleError` rather than letting a raw `NotImplementedError` reach an
API caller.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.services.penalties.dispute.types import (
    DisputeCalculation,
    DisputeFacts,
    DisputeVerdict,
    InsufficientDataForDisputeError,
    UnsupportedDisputeCalcError,
)
from app.services.penalties.projection.delay import price_delay_penalty
from app.services.penalties.projection.shortage import price_shortage_penalty
from app.services.penalties.projection.types import (
    DELAY_VIOLATION_TYPES,
    SHORTAGE_VIOLATION_TYPES,
    PenaltyRule,
)

#: Amounts within this many dollars of each other are treated as a match:
#: floating-point noise, not a genuine dispute. Same 1-cent convention as the
#: `round(..., 2)` applied to money fields throughout this codebase.
ROUNDING_TOLERANCE = 0.01


def recompute_dispute(rule: PenaltyRule, facts: DisputeFacts, claimed_amount: float) -> DisputeCalculation:
    """Deterministically recompute and classify a single penalty dispute.

    Raises `InsufficientDataForDisputeError` when a required post-delivery fact
    is missing, or `UnsupportedDisputeCalcError` when the rule's calc_type has
    no implementation for its violation family.
    """
    computed_amount, calc_trace = price_violation(rule, facts)
    computed_amount = round(computed_amount, 2)
    verdict, delta_amount = classify(computed_amount, claimed_amount)

    # Approximation: a rule's cap counts as applied when the final amount lands
    # exactly on it. Reusing the pricing functions as black boxes means this
    # engine never sees the pre-cap amount, so it cannot tell a genuine clip
    # from a penalty that coincidentally equals the cap.
    cap_amount = rule.cap_amount
    cap_applied = cap_amount is not None and computed_amount == round(cap_amount, 2)
    calc_trace = {**calc_trace, "cap_amount": cap_amount, "cap_applied": cap_applied}

    return DisputeCalculation(
        computed_amount=computed_amount,
        claimed_amount=round(claimed_amount, 2),
        delta_amount=delta_amount,
        verdict=verdict,
        calc_trace=calc_trace,
    )


def price_violation(rule: PenaltyRule, facts: DisputeFacts) -> tuple[float, dict]:
    """Price one rule's violation against real, final post-delivery facts.

    The missing-fact guard runs before any math: a violation family whose
    required fact was never recorded as of the charge date raises
    `InsufficientDataForDisputeError` rather than being priced as though the
    fact were a confirmed zero or an on-time delivery.

    The returned `calc_trace` is an audit-trail dict capturing which family and
    measure the rule used. It is never read back by the engine;
    `DisputeResolutionService.analyze` folds it into `analysis_breakdown`.
    """
    if rule.violation_type in SHORTAGE_VIOLATION_TYPES:
        if facts.delivered_qty is None:
            raise InsufficientDataForDisputeError(
                f"No delivered quantity on record as-of the charge date for a "
                f"{rule.violation_type} dispute (rule {rule.rule_id})."
            )
        shortfall_units = compute_shortfall_units(facts)
        amount = price_shortage_penalty(rule, facts.order_qty, facts.unit_price, shortfall_units)
        return amount, {
            "violation_family": "SHORTAGE",
            "shortfall_units": shortfall_units,
        }

    if rule.violation_type in DELAY_VIOLATION_TYPES:
        if facts.actual_delivery_date is None:
            raise InsufficientDataForDisputeError(
                f"No actual delivery date on record as-of the charge date for a "
                f"{rule.violation_type} dispute (rule {rule.rule_id})."
            )
        deadline = compute_deadline(facts)
        is_late = facts.actual_delivery_date > deadline
        if not is_late:
            # Delivered inside the grace-period window, so no real violation
            # occurred regardless of what was charged.
            return 0.0, {
                "violation_family": "DELAY",
                "deadline": deadline,
                "is_late": False,
            }
        days_late = compute_days_late(facts)
        try:
            amount = price_delay_penalty(rule, facts.order_qty, facts.unit_price, days_late)
        except NotImplementedError as exc:
            raise UnsupportedDisputeCalcError(str(exc)) from exc
        return amount, {
            "violation_family": "DELAY",
            "deadline": deadline,
            "is_late": True,
            "days_late": days_late,
        }

    raise ValueError(
        f"Rule {rule.rule_id} has violation_type={rule.violation_type!r}, not mapped to either "
        "SHORTAGE_VIOLATION_TYPES or DELAY_VIOLATION_TYPES."
    )


def classify(
    computed_amount: float, claimed_amount: float, tolerance: float = ROUNDING_TOLERANCE
) -> tuple[DisputeVerdict, float]:
    """Classify a dispute from the recomputed amount against the retailer's claim.

    Branch order matters: the zero check precedes the tolerance check, so a real
    violation recomputing to exactly zero is always NO_PAY even in the
    degenerate case where the claim is also near zero. A negative delta (the
    retailer undercharged) is recorded for audit only and never volunteered as a
    reason to pay more, which is a locked decision.
    """
    delta = round(claimed_amount - computed_amount, 2)

    if computed_amount == 0:
        return DisputeVerdict.NO_PAY, delta
    if abs(delta) <= tolerance:
        return DisputeVerdict.PAY_FULL, delta
    if delta > tolerance:
        return DisputeVerdict.PAY_PARTIAL, delta
    return DisputeVerdict.PAY_FULL, delta


def compute_shortfall_units(facts: DisputeFacts) -> float:
    """Real, final shortfall; caller must have already confirmed `facts.delivered_qty is not None`."""
    assert facts.delivered_qty is not None
    return max(0.0, facts.order_qty - facts.delivered_qty)


def compute_deadline(facts: DisputeFacts) -> date:
    """Last date delivery could land without being late, grace period included."""
    return facts.required_delivery_date + timedelta(days=facts.grace_period_days)


def compute_days_late(facts: DisputeFacts) -> int:
    """Calendar days between actual delivery and the grace-adjusted deadline, floored at 0.

    Caller must have already confirmed `facts.actual_delivery_date is not None`.
    """
    assert facts.actual_delivery_date is not None
    return max(0, (facts.actual_delivery_date - compute_deadline(facts)).days)


def compute_is_late(facts: DisputeFacts) -> bool:
    """Caller must have already confirmed `facts.actual_delivery_date is not None`."""
    assert facts.actual_delivery_date is not None
    return facts.actual_delivery_date > compute_deadline(facts)
