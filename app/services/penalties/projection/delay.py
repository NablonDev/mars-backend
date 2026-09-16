"""Delay probability and pricing calculations."""

from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from app.services.penalties.projection.shortage import price_tiered
from app.services.penalties.projection.types import (
    APPLIES_PER_DAY,
    APPLIES_PER_MONTH,
    APPLIES_PER_QUARTER,
    APPLIES_PER_WEEK,
    APPLIES_PER_YEAR,
    AppointmentStatus,
    CalcType,
    OrderSnapshot,
    PenaltyRule,
    ProductionStatus,
)

# Calendar-day length assumed for one fractional-period accrual unit. 30/360-style
# convention (a 30-day month, a 3-month/90-day quarter, a 365-day year): documented
# here rather than derived from a real calendar, since `days_late` has no calendar
# dates of its own to anchor a real month/quarter length against.
_PERIOD_DAYS = {
    APPLIES_PER_WEEK: 7,
    APPLIES_PER_MONTH: 30,
    APPLIES_PER_QUARTER: 90,
    APPLIES_PER_YEAR: 365,
}

# (bucket key computed from buffer_days) x (stage 1-4) -> base probability
DELAY_PROBABILITY_TABLE = {
    "ge_0": {1: 0.05, 2: 0.05, 3: 0.05, 4: 0.02},
    "eq_-1": {1: 0.15, 2: 0.30, 3: 0.50, 4: 0.85},
    "eq_-2": {1: 0.30, 2: 0.50, 3: 0.70, 4: 0.95},
    "le_-3": {1: 0.50, 2: 0.70, 3: 0.88, 4: 0.98},
}

# Production risk is a leading indicator for ship-date slip, not just for
# shortage. Deliberately modest, and last in resolve_expected_ship_date()'s
# priority order: a real appointment reschedule, an actual ship date, or an
# explicit caller estimate all carry better information than a generic
# status-based guess. Mirrors ANTICIPATED_SHORTFALL_PCT on the shortage side.
PRODUCTION_STATUS_SHIP_SLIP_DAYS = {
    ProductionStatus.ON_TRACK: 0,
    ProductionStatus.AT_RISK: 1,
    ProductionStatus.BEHIND: 2,
}


def resolve_expected_ship_date(s: OrderSnapshot) -> date:
    """Best current estimate of the ship date.

    Sources are checked most-to-least trustworthy: actual date, caller-supplied
    estimate, missed appointment, then a generic production-status guess.
    """
    if s.actual_ship_date is not None:
        return s.actual_ship_date
    if s.expected_ship_date is not None:
        return s.expected_ship_date
    if s.appointment_status == AppointmentStatus.MISSED:
        return s.required_ship_date + timedelta(days=1)
    slip = PRODUCTION_STATUS_SHIP_SLIP_DAYS[s.production_status]
    return s.required_ship_date + timedelta(days=slip)


def compute_stage(s: OrderSnapshot) -> int:
    """Classify how close the order is to shipping, as a 1-4 column key into `DELAY_PROBABILITY_TABLE`.

    Stage 4 short-circuits once `actual_ship_date` is set: a confirmed buffer
    predicts better than any estimate. Later stages carry higher base
    probabilities for the same buffer, reflecting less time left to recover.
    """
    if s.actual_ship_date is not None:
        return 4
    days_before_ship = (s.required_ship_date - s.projection_date).days
    if days_before_ship > 4:
        return 1
    if days_before_ship >= 2:
        return 2
    return 3


def compute_delay_probability(s: OrderSnapshot) -> float:
    """Estimate the probability this order misses its requested delivery date.

    Capped at 0.98 rather than treated as a certainty: a shipment already in
    transit can still arrive early.
    """
    expected_ship = resolve_expected_ship_date(s)
    expected_delivery = expected_ship + timedelta(days=s.expected_transit_days)
    buffer_days = (s.requested_delivery_date - expected_delivery).days

    stage = compute_stage(s)
    base = DELAY_PROBABILITY_TABLE[_buffer_bucket(buffer_days)][stage]
    prob = base * _carrier_multiplier(s.carrier_reliability_score)
    return min(0.98, prob)


def compute_days_late(s: OrderSnapshot) -> int:
    """Calendar days the expected delivery falls after the requested delivery date, floored at 0.

    Shares `resolve_expected_ship_date` and `expected_transit_days` with
    `compute_delay_probability`, so a day-count-accrued penalty and the
    probability it's weighted by are always derived from the same estimate.
    """
    expected_ship = resolve_expected_ship_date(s)
    expected_delivery = expected_ship + timedelta(days=s.expected_transit_days)
    return max(0, (expected_delivery - s.requested_delivery_date).days)


def price_delay_penalty(rule: PenaltyRule, order_qty: int, unit_price: float, days_late: int = 0) -> float:
    """Price a delay violation, accruing per day/week/month/quarter/year, or flat otherwise.

    TIERED bands `days_late` directly through `shortage.price_tiered` (the tier bands
    already measure the full days-late count, so no further day-count multiplication
    applies on top). Every other calc_type computes its single-application amount
    first and only then applies accrual, so `cap_amount` clamps the accrued total,
    never a single period's amount: a 4%/day rate against a 20%-of-basis cap binds
    once the running total crosses the cap, not on day one. `basis_type` doesn't
    branch the PERCENT_OF_PO amount here: this engine has one `unit_price` field, not
    separate cost and sale prices, so `BASIS_ORDER_VALUE`/`BASIS_PO_VALUE` and the
    unset legacy basis all price off `order_qty * unit_price`. `applies_per` in
    `APPLIES_PER_COUNTING_GRANULARITY` (UNIT/CASE/PALLET/SHIPMENT/DELIVERY) or
    `APPLIES_PER_OCCURRENCE` price as a single flat application, same as the default:
    those values define what a unit means (order_qty's own counting granularity, or a
    single late-delivery instance), not a second multiplier layered on top of the
    per-unit/per-PO amount already computed above. Uses `Decimal` for the
    rate/basis/day-count arithmetic so a repeating-binary rate like 0.04 can't drift
    the accrued total by a cent.
    """
    if rule.calc_type == CalcType.TIERED:
        penalty = Decimal(str(price_tiered(rule, float(days_late), order_qty * unit_price)))
        if rule.cap_amount is not None:
            penalty = min(penalty, Decimal(str(rule.cap_amount)))
        return float(penalty)

    if rule.calc_type == CalcType.PERCENT_OF_PO:
        penalty = Decimal(str(rule.rate)) * Decimal(order_qty) * Decimal(str(unit_price))
    elif rule.calc_type == CalcType.FLAT_FEE:
        penalty = Decimal(str(rule.rate))
    elif rule.calc_type == CalcType.PER_UNIT:
        penalty = Decimal(str(rule.rate)) * Decimal(order_qty)
    else:
        raise NotImplementedError(f"calc_type={rule.calc_type} not supported for delay rule {rule.rule_id}")

    if rule.applies_per == APPLIES_PER_DAY:
        penalty *= Decimal(days_late)
    elif rule.applies_per in _PERIOD_DAYS:
        penalty *= _accrual_periods(rule, days_late)
    # else: APPLIES_PER_OCCURRENCE and APPLIES_PER_COUNTING_GRANULARITY values price as
    # a single flat application; see the docstring above.

    if rule.cap_amount is not None:
        penalty = min(penalty, Decimal(str(rule.cap_amount)))
    return float(penalty)


def _buffer_bucket(buffer_days: int) -> str:
    """Map slack days onto a `DELAY_PROBABILITY_TABLE` row key.

    `buffer_days` is requested delivery minus expected delivery, so negative
    means late. Three or more days over collapse into one ceiling bucket;
    marginal risk beyond that point isn't modeled.
    """
    if buffer_days >= 0:
        return "ge_0"
    if buffer_days == -1:
        return "eq_-1"
    if buffer_days == -2:
        return "eq_-2"
    return "le_-3"


def _carrier_multiplier(reliability_score: float) -> float:
    """Scale the base delay probability up for a less reliable carrier.

    Multiplies `compute_delay_probability`'s table lookup, so a poor carrier
    amplifies buffer- and stage-driven risk rather than replacing it.
    """
    if reliability_score >= 90:
        return 1.0
    if reliability_score >= 75:
        return 1.2
    if reliability_score >= 60:
        return 1.5
    return 2.0


def _accrual_periods(rule: PenaltyRule, days_late: int) -> Decimal:
    """Convert `days_late` into a period count for a WEEK/MONTH/QUARTER/YEAR `applies_per` rule.

    `rounding_convention` decides how a partial period counts: ROUND_UP_TO_PERIOD charges a
    full period for any partial one (ceiling), ROUND_DOWN_TO_PERIOD charges only completed
    periods (floor), NEAREST_PERIOD rounds to the closest whole period, and PRORATE_EXACT
    charges the exact fractional period with no rounding at all. Hand-computable example:
    10 days late at applies_per=WEEK (a 7-day period) is 10/7 ~= 1.4286 periods ->
    ROUND_UP_TO_PERIOD prices 2, ROUND_DOWN_TO_PERIOD and NEAREST_PERIOD both price 1, and
    PRORATE_EXACT prices the exact 1.4286. Raises NotImplementedError for a rule with no
    rounding_convention or an unrecognized one, rather than silently guessing.
    """
    assert rule.applies_per in _PERIOD_DAYS  # caller only reaches here for a fractional-period rule
    periods = Decimal(days_late) / Decimal(_PERIOD_DAYS[rule.applies_per])
    if rule.rounding_convention == "ROUND_UP_TO_PERIOD":
        return periods.to_integral_value(rounding=ROUND_CEILING)
    if rule.rounding_convention == "ROUND_DOWN_TO_PERIOD":
        return periods.to_integral_value(rounding=ROUND_FLOOR)
    if rule.rounding_convention == "NEAREST_PERIOD":
        return periods.to_integral_value(rounding=ROUND_HALF_UP)
    if rule.rounding_convention == "PRORATE_EXACT":
        return periods
    raise NotImplementedError(
        f"rounding_convention={rule.rounding_convention!r} not supported for "
        f"applies_per={rule.applies_per!r} on rule {rule.rule_id}"
    )
