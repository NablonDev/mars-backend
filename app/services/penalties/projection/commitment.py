"""Volume-commitment shortfall probability, pricing, and measurement-window resolution.

Separate from `shortage.py`/`delay.py`: a volume commitment (`engine_family=
VOLUME_COMMITMENT`) is measured over a whole contract-period window for one retailer
agreement, not one purchase order, so it needs its own probability model (run-rate
extrapolation, not the shortage/delay scoring tables) and its own pricing entry point,
reached only through `ProjectionService.run_commitment_projection`, never the per-PO
`ProjectionEngine.project` loop.
"""

import calendar
from datetime import date, timedelta

from app.services.penalties.projection.shortage import price_tiered
from app.services.penalties.projection.types import CalcType, CommitmentSnapshot, PenaltyRule

# Ceiling mirroring SHORTAGE_LOCKED_IN_PROBABILITY/compute_delay_probability's 0.98 cap: a
# projected shortfall is never priced as a certainty, since purchasing pace can still
# recover before the window closes.
COMMITMENT_SHORTFALL_PROBABILITY_CAP = 0.95

# Calendar-day length per measurement_window_unit, matching delay.py's own 30/360-style
# convention (a 30-day month, a 90-day quarter, a 365-day year).
_UNIT_DAYS = {"DAYS": 1, "WEEKS": 7, "MONTHS": 30, "QUARTERS": 90, "YEARS": 365}


def project_full_window_total(actual_to_date: float, elapsed_days: int, total_window_days: int) -> float:
    """Extrapolate an actual-to-date running total to a full-window projection.

    The run-rate model: `daily_rate = actual_to_date / elapsed_days` (the average pace
    observed so far), then `daily_rate * total_window_days` (that pace held for the
    window's whole length). Returns `actual_to_date` unchanged when `elapsed_days <= 0`:
    on or before the window's first day there is no observed pace to extrapolate from, so
    the current total is the best available estimate. Hand-computable example: 100 units
    bought in the first 10 days of a 40-day window projects to 100/10*40 = 400 units for
    the full window.
    """
    if elapsed_days <= 0:
        return actual_to_date
    daily_rate = actual_to_date / elapsed_days
    return daily_rate * total_window_days


def compute_commitment_shortfall_probability(committed: float, projected_total: float) -> float:
    """Probability of missing a volume commitment, as the projected shortfall's share of it.

    `(committed - projected_total) / committed`, floored at 0.0 (a projection at or above
    the commitment carries no shortfall risk) and capped at
    `COMMITMENT_SHORTFALL_PROBABILITY_CAP` rather than a full 1.0: purchasing pace can
    still recover before the window closes, even at a severe projected shortfall. Returns
    0.0 when `committed <= 0` (nothing to measure a shortfall against). Hand-computable
    example: committed=50,000, projected_total=38,000 -> (50,000-38,000)/50,000 = 0.24.
    """
    if committed <= 0:
        return 0.0
    shortfall_ratio = (committed - projected_total) / committed
    return min(COMMITMENT_SHORTFALL_PROBABILITY_CAP, max(0.0, shortfall_ratio))


def commitment_shortfall_probability(snapshot: CommitmentSnapshot) -> float:
    """Shortfall probability for one snapshot: quantity-based when available, else value-based.

    Convenience wrapper around `project_full_window_total` and
    `compute_commitment_shortfall_probability` for a caller that only has a
    `CommitmentSnapshot`, not the separate committed/projected figures. Returns 0.0 when
    the snapshot carries neither a quantity nor a value pair to measure against.
    """
    if snapshot.committed_quantity is not None and snapshot.actual_to_date_quantity is not None:
        elapsed_days, total_window_days = _elapsed_and_total_days(snapshot)
        projected = project_full_window_total(
            snapshot.actual_to_date_quantity, elapsed_days, total_window_days
        )
        return compute_commitment_shortfall_probability(snapshot.committed_quantity, projected)
    if snapshot.committed_value is not None and snapshot.actual_to_date_value is not None:
        elapsed_days, total_window_days = _elapsed_and_total_days(snapshot)
        projected = project_full_window_total(snapshot.actual_to_date_value, elapsed_days, total_window_days)
        return compute_commitment_shortfall_probability(snapshot.committed_value, projected)
    return 0.0


def projected_shortfall_quantity(snapshot: CommitmentSnapshot) -> float | None:
    """Run-rate-projected shortfall in quantity units, or None when the rule is value-based."""
    if snapshot.committed_quantity is None or snapshot.actual_to_date_quantity is None:
        return None
    projected = project_full_window_total(
        snapshot.actual_to_date_quantity, *_elapsed_and_total_days(snapshot)
    )
    return max(0.0, snapshot.committed_quantity - projected)


def projected_shortfall_value(snapshot: CommitmentSnapshot) -> float | None:
    """Run-rate-projected shortfall in dollar value, or None when the rule is quantity-based."""
    if snapshot.committed_value is None or snapshot.actual_to_date_value is None:
        return None
    projected = project_full_window_total(snapshot.actual_to_date_value, *_elapsed_and_total_days(snapshot))
    return max(0.0, snapshot.committed_value - projected)


def price_volume_shortfall(rule: PenaltyRule, snapshot: CommitmentSnapshot) -> float:
    """Price a VOLUME_COMMITMENT rule against the run-rate-projected shortfall.

    PER_UNIT charges `rule.rate` per projected shortfall unit (quantity-based; 0.0 when
    the rule carries no quantity commitment). PERCENT_OF_PO prices `rule.rate` times the
    commitment's own `committed_value`, the contract-period analogue of a PO's order
    value, whenever any shortfall is projected at all. TIERED bands the shortfall as a
    fraction of the commitment through `shortage.price_tiered`, shared with the per-PO
    shortage ladder. FLAT_FEE is a flat charge whenever any shortfall is projected.
    """
    shortfall_qty = projected_shortfall_quantity(snapshot)
    shortfall_value = projected_shortfall_value(snapshot)
    has_shortfall = (shortfall_qty or 0.0) > 0 or (shortfall_value or 0.0) > 0

    if rule.calc_type == CalcType.PER_UNIT:
        penalty = (shortfall_qty or 0.0) * rule.rate
    elif rule.calc_type == CalcType.FLAT_FEE:
        penalty = rule.rate if has_shortfall else 0.0
    elif rule.calc_type == CalcType.PERCENT_OF_PO:
        penalty = rule.rate * (snapshot.committed_value or 0.0) if has_shortfall else 0.0
    elif rule.calc_type == CalcType.TIERED:
        committed = snapshot.committed_quantity or snapshot.committed_value or 0.0
        shortfall = shortfall_qty if shortfall_qty is not None else (shortfall_value or 0.0)
        measure = shortfall / committed if committed else 0.0
        penalty = price_tiered(rule, measure, snapshot.committed_value or 0.0)
    else:
        raise NotImplementedError(
            f"calc_type={rule.calc_type} not supported for commitment rule {rule.rule_id}"
        )

    if rule.cap_amount is not None:
        penalty = min(penalty, rule.cap_amount)
    return penalty


def resolve_measurement_window(
    window_type: str,
    length: int,
    unit: str,
    contract_anchor_date: date,
    as_of_date: date,
) -> tuple[date, date]:
    """Resolve a rule's measurement window to concrete `[start, end]` dates as of a given date.

    ROLLING trails `as_of_date`: the window is the `length`-`unit` span ending on
    `as_of_date` itself. FIXED_CALENDAR anchors to calendar-unit boundaries (the calendar
    year/quarter/month containing `as_of_date`), independent of the contract.
    CONTRACT_YEAR and ANNIVERSARY both anchor to `contract_anchor_date` (the retailer
    agreement's `effective_date`): the window is the `contract_anchor_date`-aligned
    `length`-`unit` period containing `as_of_date`, found by stepping whole periods
    forward from the anchor. The two window types differ in how a rule typically sets
    `length`/`unit` (CONTRACT_YEAR is conventionally one year; ANNIVERSARY allows any
    period still anchored to the contract's start date), not in this calculation.
    """
    if window_type == "ROLLING":
        span_days = _UNIT_DAYS[unit] * length
        return as_of_date - timedelta(days=span_days), as_of_date

    if window_type == "FIXED_CALENDAR":
        return _fixed_calendar_window(unit, length, as_of_date)

    if window_type in ("CONTRACT_YEAR", "ANNIVERSARY"):
        return _anchored_window(contract_anchor_date, unit, length, as_of_date)

    raise ValueError(f"Unsupported measurement_window_type={window_type!r}")


def _elapsed_and_total_days(snapshot: CommitmentSnapshot) -> tuple[int, int]:
    """`(elapsed_days, total_window_days)` for a snapshot, shared by both shortfall functions."""
    elapsed_days = (snapshot.as_of_date - snapshot.window_start_date).days
    total_window_days = (snapshot.window_end_date - snapshot.window_start_date).days
    return elapsed_days, total_window_days


def _fixed_calendar_window(unit: str, length: int, as_of_date: date) -> tuple[date, date]:
    """The calendar-unit block containing `as_of_date`, `length` units wide, calendar-aligned.

    DAYS/WEEKS block on a fixed epoch (2000-01-01) in day counts, since a day/week has no
    natural calendar-aligned start otherwise. MONTHS/QUARTERS/YEARS block on whole months
    counted from month 0 (year 0, January), so `length=1` gives exactly the calendar
    month/quarter/year containing `as_of_date`.
    """
    if unit in ("DAYS", "WEEKS"):
        epoch = date(2000, 1, 1)
        block_days = _UNIT_DAYS[unit] * length
        elapsed = (as_of_date - epoch).days
        block_start = epoch + timedelta(days=(elapsed // block_days) * block_days)
        block_end = block_start + timedelta(days=block_days - 1)
        return block_start, block_end

    period_months = {"MONTHS": length, "QUARTERS": length * 3, "YEARS": length * 12}[unit]
    month_index = as_of_date.year * 12 + (as_of_date.month - 1)
    block_start_index = (month_index // period_months) * period_months
    block_start = date(block_start_index // 12, block_start_index % 12 + 1, 1)
    next_block_start = _add_months(block_start, period_months)
    return block_start, next_block_start - timedelta(days=1)


def _anchored_window(
    contract_anchor_date: date, unit: str, length: int, as_of_date: date
) -> tuple[date, date]:
    """The `contract_anchor_date`-aligned period containing `as_of_date`.

    Steps whole `length`-`unit` periods forward from `contract_anchor_date` (never
    backward past it, even if `as_of_date` precedes it) until `as_of_date` falls inside
    one, so every window boundary is a real anniversary of the contract's own start date
    rather than a calendar boundary.
    """
    if unit in ("DAYS", "WEEKS"):
        span_days = _UNIT_DAYS[unit] * length
        if as_of_date <= contract_anchor_date:
            return contract_anchor_date, contract_anchor_date + timedelta(days=span_days - 1)
        elapsed = (as_of_date - contract_anchor_date).days
        block_start = contract_anchor_date + timedelta(days=(elapsed // span_days) * span_days)
        return block_start, block_start + timedelta(days=span_days - 1)

    period_months = {"MONTHS": length, "QUARTERS": length * 3, "YEARS": length * 12}[unit]
    block_start = contract_anchor_date
    next_start = _add_months(block_start, period_months)
    if as_of_date < block_start:
        return block_start, next_start - timedelta(days=1)
    while as_of_date >= next_start:
        block_start = next_start
        next_start = _add_months(block_start, period_months)
    return block_start, next_start - timedelta(days=1)


def _add_months(d: date, months: int) -> date:
    """`d` shifted forward by a whole number of months, clamping the day to the target month's length.

    E.g. Jan 31 plus 1 month lands on Feb 28 (or 29 in a leap year), never a nonexistent
    Feb 31. `_fixed_calendar_window` only ever calls this with `d.day == 1`, where clamping
    never applies; `_anchored_window` can call it with any day-of-month from a contract's
    real `effective_date`, where it does.
    """
    month_index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(month_index, 12)
    month += 1
    last_day_of_month = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day_of_month))
