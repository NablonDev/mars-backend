"""Shortage probability and pricing calculations."""

from decimal import Decimal

from app.services.penalties.projection.types import (
    BASIS_SHORTFALL_VALUE,
    CalcType,
    OrderSnapshot,
    PenaltyRule,
    ProductionStatus,
)

# Estimated shortfall used when no confirmed cut exists.
ANTICIPATED_SHORTFALL_PCT = {
    ProductionStatus.ON_TRACK: 0.00,
    ProductionStatus.AT_RISK: 0.10,
    ProductionStatus.BEHIND: 0.25,
}

STATUS_POINTS = {
    ProductionStatus.ON_TRACK: 0,
    ProductionStatus.AT_RISK: 15,
    ProductionStatus.BEHIND: 30,
}

# Near-certain probability once a physical shipment confirms a shortfall.
SHORTAGE_LOCKED_IN_PROBABILITY = 0.95


def compute_shortage_probability(s: OrderSnapshot) -> float:
    """Estimate the probability this order ships short of its full order quantity.

    Short-circuits to `SHORTAGE_LOCKED_IN_PROBABILITY` once shipment has
    happened with a gap already confirmed. That constant is 0.95 rather than
    1.0 because a correction can still be recorded after the fact.
    """
    actual_shortfall = s.order_qty - s.confirmed_qty
    if s.actual_ship_date is not None and actual_shortfall > 0:
        return SHORTAGE_LOCKED_IN_PROBABILITY

    gap_pct = actual_shortfall / s.order_qty if s.order_qty else 0.0
    days_to_delivery = (s.requested_delivery_date - s.projection_date).days
    score = (
        _gap_points(gap_pct)
        + STATUS_POINTS[s.production_status]
        + _days_points(days_to_delivery)
        + _demand_exception_points(s)
    )
    return _score_to_probability(score)


def shortfall_units_for_pricing(s: OrderSnapshot) -> float:
    """Confirmed shortfall when one exists, otherwise a status-driven estimate."""
    actual_shortfall = s.order_qty - s.confirmed_qty
    if actual_shortfall > 0:
        return float(actual_shortfall)
    return ANTICIPATED_SHORTFALL_PCT[s.production_status] * s.order_qty


def price_shortage_penalty(
    rule: PenaltyRule, order_qty: int, unit_price: float, shortfall_units: float
) -> float:
    """Price a shortage violation's monetary penalty under one rule's calc_type.

    `threshold_pct` is a grace band: only the shortfall beyond
    `threshold_pct * order_qty` is penalized, and a shortfall fully inside the
    band prices to 0.0 regardless of calc_type. `basis_type=BASIS_SHORTFALL_VALUE`
    is the one exception: it's priced by `_price_shortfall_value_basis`
    instead, with `threshold_pct` read as a fill-rate floor rather than a
    grace band.
    """
    if rule.basis_type == BASIS_SHORTFALL_VALUE:
        return _price_shortfall_value_basis(rule, order_qty, unit_price, shortfall_units)

    threshold_units = rule.threshold_pct * order_qty
    penalized_units = max(0.0, shortfall_units - threshold_units)
    if penalized_units <= 0:
        return 0.0

    if rule.calc_type == CalcType.PER_UNIT:
        penalty = penalized_units * rule.rate
    elif rule.calc_type == CalcType.PERCENT_OF_PO:
        # Flat once breached; does not scale with shortfall size. basis_type
        # BASIS_COST_OF_GOODS prices the same way: this engine has one
        # unit_price field, not separate cost and sale prices.
        penalty = rule.rate * order_qty * unit_price
    elif rule.calc_type == CalcType.FLAT_FEE:
        penalty = rule.rate
    elif rule.calc_type == CalcType.TIERED:
        # Tiered shortage penalties are based on shortfall percentage of PO value.
        gap_pct = shortfall_units / order_qty if order_qty else 0.0
        penalty = _price_tiered(rule, gap_pct, order_qty * unit_price)
    else:
        raise NotImplementedError(f"Unsupported calc_type for rule {rule.rule_id}")

    if rule.cap_amount is not None:
        penalty = min(penalty, rule.cap_amount)
    return penalty


def _gap_points(gap_pct: float) -> int:
    """Score the confirmed shortfall gap, as a fraction of order_qty, toward the composite risk score.

    Ceilings at 40, weighting an already-confirmed cut above the status and
    days-to-delivery components, which are weaker evidence.
    """
    if gap_pct <= 0:
        return 0
    if gap_pct < 0.10:
        return 10
    if gap_pct <= 0.30:
        return 25
    return 40


def _days_points(days_to_delivery: int) -> int:
    """Score how little time remains to close a shortfall before the requested delivery date."""
    if days_to_delivery >= 8:
        return 0
    if days_to_delivery >= 4:
        return 10
    if days_to_delivery >= 1:
        return 20
    return 30


def _demand_exception_points(s: OrderSnapshot) -> int:
    """Add a small early-warning score for a flagged demand exception.

    Counts only while the order is still ON_TRACK with quantity fully
    confirmed. Once status escalates or a real gap opens, `_gap_points` and
    `STATUS_POINTS` capture the same signal, and scoring it here as well
    would overweight it.
    """
    if (
        s.demand_exception_flagged
        and s.production_status == ProductionStatus.ON_TRACK
        and s.confirmed_qty >= s.order_qty
    ):
        return 5
    return 0


def _score_to_probability(score: int) -> float:
    """Map the composite shortage risk score onto a calibrated probability.

    Anything above the top band falls through to 0.92, never a full 1.0: even
    a heavily-scored order can still recover before requested delivery.
    """
    bands = [(10, 0.05), (25, 0.15), (45, 0.35), (65, 0.55), (85, 0.75)]
    for upper, prob in bands:
        if score <= upper:
            return prob
    return 0.92


def _price_shortfall_value_basis(
    rule: PenaltyRule, order_qty: int, unit_price: float, shortfall_units: float
) -> float:
    """Price a `basis_type=BASIS_SHORTFALL_VALUE` rule against the missing units' invoice value.

    `threshold_pct` means something different here than the grace band the
    other bases use: it's the minimum required fill rate, not a quantity of
    shortfall forgiven. Below it, the rule fires on the full shortfall value,
    undiminished; at or above it (the boundary is compliant, not a breach),
    it prices to 0.0. Uses `Decimal` for the rate/basis arithmetic so a
    repeating-binary rate like 0.07 can't drift the result by a cent.
    """
    fill_rate = (order_qty - shortfall_units) / order_qty if order_qty else 1.0
    if fill_rate >= rule.threshold_pct:
        return 0.0

    basis_amount = Decimal(str(shortfall_units)) * Decimal(str(unit_price))
    penalty = Decimal(str(rule.rate)) * basis_amount
    if rule.cap_amount is not None:
        penalty = min(penalty, Decimal(str(rule.cap_amount)))
    return float(penalty)


def _price_tiered(rule: PenaltyRule, measure: float, po_value: float) -> float:
    """Price a TIERED rule from the single band containing `measure`.

    Bands are half-open (`band_min <= measure < band_max`), so no boundary is
    ambiguous. Returns 0.0 both for a rule with no tiers and for a `measure`
    above every band: a gap left at the top means no penalty, not an error.
    """
    if not rule.tiers:
        return 0.0
    for tier in rule.tiers:
        if tier.band_min <= measure < tier.band_max:
            return tier.rate * po_value
    return 0.0
