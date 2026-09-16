"""Deterministic dispute recompute-and-classify engine.

Reuses the projection engine's pricing functions (`price_shortage_penalty`,
`price_delay_penalty`, `price_tiered`) unchanged, fed with real post-delivery facts rather
than risk-adjusted estimates. Pricing logic is never reimplemented here; this module
computes only how much of a rule's shortfall, delay, or claim-supplied measure actually
happened. `shortfall_units_for_pricing` is deliberately never called, because
its `ANTICIPATED_SHORTFALL_PCT` fallback is a pre-delivery risk estimate and is
meaningless once delivery is final.

The LLM never decides pay, no-pay, or how much; this module alone does.
`DisputeResolutionService.analyze` is its only caller, and the dispute-summary agent only
narrates a verdict already computed and persisted here.

`price_violation` dispatches on the rule's `engine_family` (falling back to the legacy
`violation_type` set membership for a rule published before Phase 1 or seeded directly, so
existing SHORTAGE/DELAY rules keep pricing exactly as before). Each family maps to a
measure function in `_FAMILY_MEASURE_FNS`, built at the bottom of this module once every
measure function is defined; `_require_family_keys` checks `DisputeFacts.
FAMILY_REQUIRED_KEYS` before any measure function runs, so a missing fact always raises
`InsufficientDataForDisputeError` rather than being priced as a confirmed zero. A family
with no dispatch entry (or a `calc_type` its measure function doesn't branch on) raises
`UnsupportedDisputeCalcError`, which `DisputeResolutionService.analyze` turns into a
`BusinessRuleError` rather than letting a raw exception reach an API caller.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from app.services.penalties.dispute.types import (
    FAMILY_REQUIRED_KEYS,
    DisputeCalculation,
    DisputeFacts,
    DisputeVerdict,
    InsufficientDataForDisputeError,
    UnsupportedDisputeCalcError,
)
from app.services.penalties.projection.delay import price_delay_penalty
from app.services.penalties.projection.shortage import price_shortage_penalty, price_tiered
from app.services.penalties.projection.types import (
    DELAY_VIOLATION_TYPES,
    ENGINE_FAMILY_DELAY,
    ENGINE_FAMILY_SHORTAGE,
    ENGINE_FAMILY_VOLUME_COMMITMENT,
    SHORTAGE_VIOLATION_TYPES,
    CalcType,
    PenaltyRule,
)

#: Amounts within this many dollars of each other are treated as a match:
#: floating-point noise, not a genuine dispute. Same 1-cent convention as the
#: `round(..., 2)` applied to money fields throughout this codebase.
ROUNDING_TOLERANCE = 0.01

_MeasureFn = Callable[[PenaltyRule, DisputeFacts], tuple[float, dict]]


def recompute_dispute(rule: PenaltyRule, facts: DisputeFacts, claimed_amount: float) -> DisputeCalculation:
    """Deterministically recompute and classify a single penalty dispute.

    Raises `InsufficientDataForDisputeError` when a required fact is missing, or
    `UnsupportedDisputeCalcError` when the rule's family/calc_type has no recompute path.
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
    """Price one rule's violation against real, final post-delivery/claim facts.

    The missing-fact guard (`_require_family_keys`) runs before any math: a family whose
    required fact was never recorded raises `InsufficientDataForDisputeError` rather than
    being priced as though the fact were a confirmed zero. The returned `calc_trace` is an
    audit-trail dict, including which fact keys were claim-supplied vs Mars-derived for
    this call; it is never read back by the engine, `DisputeResolutionService.analyze`
    folds it into `analysis_breakdown`.
    """
    family = _resolve_family(rule)
    if family is None or family not in _FAMILY_MEASURE_FNS:
        raise UnsupportedDisputeCalcError(
            f"Rule {rule.rule_id} has violation_type={rule.violation_type!r} "
            f"(engine_family={rule.engine_family!r}), not mapped to either "
            "SHORTAGE_VIOLATION_TYPES or DELAY_VIOLATION_TYPES nor to any dispute dispatch "
            "family. Dispute has no general recompute path for this family yet (see "
            "docs/architecture/extraction-engine-integration-plan.md Phase 4)."
        )
    _require_family_keys(rule, facts, family)
    return _FAMILY_MEASURE_FNS[family](rule, facts)


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


def generic_recompute(rule: PenaltyRule, measure: float, basis_amount: float) -> float:
    """Recompute a family with no bespoke measure function against the rule's own calc_type.

    Mirrors `price_shortage_penalty`'s calc_type branches with no `basis_type` special
    case: no family routed here needs shortage.py's SHORTFALL_VALUE/SHORTFALL_UNITS
    branches. PERCENT_OF_PO and FLAT_FEE are flat once `measure` confirms a violation
    occurred (matching the projection precedent), not scaled by its size.
    """
    if measure <= 0:
        return 0.0
    if rule.calc_type == CalcType.PER_UNIT:
        amount = measure * rule.rate
    elif rule.calc_type == CalcType.PERCENT_OF_PO:
        amount = rule.rate * basis_amount
    elif rule.calc_type == CalcType.FLAT_FEE:
        amount = rule.rate
    elif rule.calc_type == CalcType.TIERED:
        amount = price_tiered(rule, measure, basis_amount)
    else:
        raise UnsupportedDisputeCalcError(
            f"calc_type={rule.calc_type} not supported by the generic dispute recompute (rule {rule.rule_id})"
        )
    if rule.cap_amount is not None:
        amount = min(amount, rule.cap_amount)
    return amount


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


def _resolve_family(rule: PenaltyRule) -> str | None:
    """One rule's dispute-dispatch family: `engine_family` when set, else the legacy
    `violation_type` set membership for a rule published before Phase 1 or seeded directly.
    """
    if rule.engine_family is not None:
        return rule.engine_family
    if rule.violation_type in SHORTAGE_VIOLATION_TYPES:
        return ENGINE_FAMILY_SHORTAGE
    if rule.violation_type in DELAY_VIOLATION_TYPES:
        return ENGINE_FAMILY_DELAY
    return None


def _require_family_keys(rule: PenaltyRule, facts: DisputeFacts, family: str) -> None:
    """Raise `InsufficientDataForDisputeError` if any of `family`'s required facts are unset.

    Checks both `claim_supplied_keys` and `mars_derived_keys` from `FAMILY_REQUIRED_KEYS`:
    absence must never collapse into a confirmed zero or default. QUALITY carries no
    entries here; `_price_quality` runs its own conditional check instead (see
    `DisputeFamilyKeys`'s docstring).
    """
    requirement = FAMILY_REQUIRED_KEYS.get(family)
    if requirement is None:
        return
    missing = [
        key
        for key in (*requirement.claim_supplied_keys, *requirement.mars_derived_keys)
        if getattr(facts, key) is None
    ]
    if missing:
        raise InsufficientDataForDisputeError(
            f"No {', '.join(missing)} on record for a {family} dispute (rule {rule.rule_id})."
        )


def _price_shortage_family(rule: PenaltyRule, facts: DisputeFacts) -> tuple[float, dict]:
    """SHORTAGE: reuses `price_shortage_penalty` against the real, final shortfall."""
    shortfall_units = compute_shortfall_units(facts)
    amount = price_shortage_penalty(rule, facts.order_qty, facts.unit_price, shortfall_units)
    return amount, {
        "violation_family": "SHORTAGE",
        "shortfall_units": shortfall_units,
        "claim_supplied_keys": [],
        "mars_derived_keys": ["delivered_qty"],
    }


def _price_delay_family(rule: PenaltyRule, facts: DisputeFacts) -> tuple[float, dict]:
    """DELAY: reuses `price_delay_penalty` against the real, final delivery date."""
    assert facts.actual_delivery_date is not None  # guarded by _require_family_keys
    deadline = compute_deadline(facts)
    is_late = facts.actual_delivery_date > deadline
    if not is_late:
        # Delivered inside the grace-period window, so no real violation
        # occurred regardless of what was charged.
        return 0.0, {
            "violation_family": "DELAY",
            "deadline": deadline,
            "is_late": False,
            "claim_supplied_keys": [],
            "mars_derived_keys": ["actual_delivery_date"],
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
        "claim_supplied_keys": [],
        "mars_derived_keys": ["actual_delivery_date"],
    }


def _price_quality(rule: PenaltyRule, facts: DisputeFacts) -> tuple[float, dict]:
    """QUALITY: PERCENT_OF_PO rules price off `defect_rate_pct`, everything else off `defect_units`.

    Both are facts Mars genuinely has no other record of (a rate or a count the retailer's
    DC inspection reported); `basis_amount` is always the PO's own Mars-derived order value.
    """
    basis_amount = facts.order_qty * facts.unit_price
    if rule.calc_type == CalcType.PERCENT_OF_PO:
        if facts.defect_rate_pct is None:
            raise InsufficientDataForDisputeError(
                f"No defect_rate_pct on the claim for a QUALITY dispute against a "
                f"PERCENT_OF_PO rule (rule {rule.rule_id})."
            )
        measure, claim_key = facts.defect_rate_pct, "defect_rate_pct"
    else:
        if facts.defect_units is None:
            raise InsufficientDataForDisputeError(
                f"No defect_units on the claim for a QUALITY dispute (rule {rule.rule_id})."
            )
        measure, claim_key = facts.defect_units, "defect_units"
    amount = generic_recompute(rule, measure, basis_amount)
    return amount, {
        "violation_family": "QUALITY",
        "measure": measure,
        "claim_supplied_keys": [claim_key],
        "mars_derived_keys": ["order_qty", "unit_price"],
    }


def _price_cover_purchase(rule: PenaltyRule, facts: DisputeFacts) -> tuple[float, dict]:
    """COVER_PURCHASE: prices the excess of the claim-supplied cover cost over Mars's own PO value.

    `replacement_cost_paid` is claim-supplied (what the retailer actually paid a substitute
    vendor; Mars has no record of it). `original_cost` is Mars-derived, computed by
    `DisputeResolutionService.analyze` from the PO's own order_qty * unit_price, never read
    from claim_facts even if a claim happens to assert one.
    """
    assert facts.replacement_cost_paid is not None  # guarded by _require_family_keys
    assert facts.original_cost is not None
    markup = max(0.0, facts.replacement_cost_paid - facts.original_cost)
    amount = generic_recompute(rule, markup, facts.original_cost)
    return amount, {
        "violation_family": "COVER_PURCHASE",
        "replacement_cost_paid": facts.replacement_cost_paid,
        "original_cost": facts.original_cost,
        "markup": markup,
        "claim_supplied_keys": ["replacement_cost_paid"],
        "mars_derived_keys": ["original_cost"],
    }


def _make_generic_family(claim_key: str, family: str) -> _MeasureFn:
    """Build a measure function for a family priced off exactly one claim-supplied fact.

    Shared by FINANCIAL/STORAGE_DURATION_FEE: each needs one claim-supplied count Mars has
    no other record of (an occurrence count, a day count), priced against the PO's own
    Mars-derived order value as basis.
    """

    def measure_fn(rule: PenaltyRule, facts: DisputeFacts) -> tuple[float, dict]:
        measure = getattr(facts, claim_key)
        basis_amount = facts.order_qty * facts.unit_price
        amount = generic_recompute(rule, measure, basis_amount)
        return amount, {
            "violation_family": family,
            claim_key: measure,
            "claim_supplied_keys": [claim_key],
            "mars_derived_keys": ["order_qty", "unit_price"],
        }

    return measure_fn


def _price_volume_commitment(rule: PenaltyRule, facts: DisputeFacts) -> tuple[float, dict]:
    """VOLUME_COMMITMENT: recomputes from Mars's own committed/actual purchase totals.

    Both `committed_quantity` (the matched rule's own `commitment_quantity`) and
    `actual_purchase_quantity` (`PurchaseOrderRepository.get_ordered_totals_for_retailer_between`'s
    own total) are Mars-derived; never read from claim_facts, even when a claim asserts a
    conflicting purchase total (decision #2/4f). Mirrors
    `app.services.penalties.projection.commitment.price_volume_shortfall`'s calc_type
    branches, but against a final, already-elapsed total rather than a run-rate projection:
    a dispute adjudicates a charge already made, not a forecast.
    """
    assert facts.committed_quantity is not None  # guarded by _require_family_keys
    assert facts.actual_purchase_quantity is not None
    shortfall_qty = max(0.0, facts.committed_quantity - facts.actual_purchase_quantity)
    basis_amount = rule.commitment_value or 0.0

    if rule.calc_type == CalcType.PER_UNIT:
        amount = shortfall_qty * rule.rate
    elif rule.calc_type == CalcType.FLAT_FEE:
        amount = rule.rate if shortfall_qty > 0 else 0.0
    elif rule.calc_type == CalcType.PERCENT_OF_PO:
        amount = rule.rate * basis_amount if shortfall_qty > 0 else 0.0
    elif rule.calc_type == CalcType.TIERED:
        measure = shortfall_qty / facts.committed_quantity if facts.committed_quantity else 0.0
        amount = price_tiered(rule, measure, basis_amount)
    else:
        raise UnsupportedDisputeCalcError(
            f"calc_type={rule.calc_type} not supported for VOLUME_COMMITMENT dispute (rule {rule.rule_id})"
        )
    if rule.cap_amount is not None:
        amount = min(amount, rule.cap_amount)
    return amount, {
        "violation_family": "VOLUME_COMMITMENT",
        "committed_quantity": facts.committed_quantity,
        "actual_purchase_quantity": facts.actual_purchase_quantity,
        "shortfall_quantity": shortfall_qty,
        "claim_supplied_keys": [],
        "mars_derived_keys": ["committed_quantity", "actual_purchase_quantity"],
    }


# Family -> measure function, built after every measure function above is defined. Extend
# this (plus FAMILY_REQUIRED_KEYS in dispute/types.py) to admit a new dispute-priceable
# family; an entry missing here is not a bug, it is decision #4's documented backlog: the
# family still publishes (Phase 1) and still projects/mitigates if shortage/delay-shaped
# (Phase 3), it just cannot be disputed yet, and stays OPEN via UnsupportedDisputeCalcError.
_FAMILY_MEASURE_FNS: dict[str, _MeasureFn] = {
    ENGINE_FAMILY_SHORTAGE: _price_shortage_family,
    ENGINE_FAMILY_DELAY: _price_delay_family,
    "QUALITY": _price_quality,
    "COVER_PURCHASE": _price_cover_purchase,
    "FINANCIAL": _make_generic_family("occurrence_count", "FINANCIAL"),
    "STORAGE_DURATION_FEE": _make_generic_family("storage_days", "STORAGE_DURATION_FEE"),
    ENGINE_FAMILY_VOLUME_COMMITMENT: _price_volume_commitment,
}
