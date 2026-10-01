"""Supply position engine: allocating on-hand stock and receipts to demand.

Pure, framework-free calculation: given on-hand stock, a set of upstream
receipts (production-order or QA-lot completions), and the competing
fulfillment-plan demands for the same (material, plant), allocates supply to
demand in need-date order and reports each demand line's coverage and each
plan's resulting shortfall and earliest-possible material-available date.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

TOLERANCE = 1e-6

DEMAND_EXCEEDS_SUPPLY = "DEMAND_EXCEEDS_SUPPLY"
SUPPLY_DELAYED = "SUPPLY_DELAYED"


@dataclass(frozen=True)
class SupplyReceipt:
    source_type: str
    source_id: str
    quantity: float
    available_date: date
    baseline_available_date: date | None
    reason_code: str | None


@dataclass(frozen=True)
class SupplyDemand:
    plan_id: str
    plan_line_id: str
    quantity: float
    need_date: date
    order_date: date
    plan_number: str


@dataclass(frozen=True)
class DemandCoverage:
    plan_id: str
    plan_line_id: str
    required_quantity: float
    on_time_quantity: float
    full_cover_date: date | None
    uncovered_quantity: float
    cause_code: str | None
    cause_source_id: str | None
    cause_available_date: date | None = None
    cause_baseline_date: date | None = None
    on_hand: float = 0.0
    # Date of the latest late bucket this demand drew from, even if it was only partly covered.
    last_late_date: date | None = None


@dataclass(frozen=True)
class PlanSupplyOutcome:
    plan_id: str
    shortfall_quantity: float
    material_available_not_before: date | None
    cause_code: str | None
    cause_source_id: str | None
    full_cover_date: date | None
    required_quantity: float = 0.0
    on_time_quantity: float = 0.0
    on_hand: float = 0.0
    cause_available_date: date | None = None
    cause_baseline_date: date | None = None


@dataclass
class _Bucket:
    date: date
    source_id: str
    remaining: float
    receipt: SupplyReceipt | None = field(default=None)


def allocate_supply(
    on_hand: float, receipts: Sequence[SupplyReceipt], demands: Sequence[SupplyDemand], as_of: date
) -> list[DemandCoverage]:
    """Allocate `on_hand` and `receipts` to `demands`, earliest need date first."""
    buckets = _build_buckets(on_hand, receipts, as_of)
    ordered_demands = sorted(
        demands, key=lambda d: (d.need_date, d.order_date, d.plan_number, d.plan_line_id)
    )
    return [_allocate_one(demand, buckets, on_hand) for demand in ordered_demands]


def plan_outcomes(coverages: Sequence[DemandCoverage]) -> list[PlanSupplyOutcome]:
    """Roll demand-line coverage up to a shortfall/not-before outcome per plan."""
    return [_build_plan_outcome(plan_id, lines) for plan_id, lines in _group_by_plan(coverages).items()]


def _build_buckets(on_hand: float, receipts: Sequence[SupplyReceipt], as_of: date) -> list[_Bucket]:
    buckets = [_Bucket(date=as_of, source_id="", remaining=on_hand)]
    buckets.extend(
        _Bucket(
            date=receipt.available_date,
            source_id=receipt.source_id,
            remaining=receipt.quantity,
            receipt=receipt,
        )
        for receipt in receipts
    )
    buckets.sort(key=lambda b: (b.date, b.source_id))
    return buckets


def _allocate_one(demand: SupplyDemand, buckets: list[_Bucket], on_hand: float) -> DemandCoverage:
    required = demand.quantity
    on_time = 0.0
    consumed = 0.0

    for bucket in buckets:
        if consumed >= required - TOLERANCE:
            break
        if bucket.date <= demand.need_date and bucket.remaining > TOLERANCE:
            take = min(bucket.remaining, required - consumed)
            bucket.remaining -= take
            consumed += take
            on_time += take

    last_late_date: date | None = None
    for bucket in buckets:
        if consumed >= required - TOLERANCE:
            break
        if bucket.date > demand.need_date and bucket.remaining > TOLERANCE:
            take = min(bucket.remaining, required - consumed)
            bucket.remaining -= take
            consumed += take
            last_late_date = bucket.date

    fully_covered = consumed >= required - TOLERANCE
    if not fully_covered:
        full_cover_date = None
    elif on_time >= required - TOLERANCE:
        full_cover_date = demand.need_date
    else:
        full_cover_date = last_late_date

    uncovered = _zero_if_close(required - consumed)
    cause_code, cause_source_id, cause_available_date, cause_baseline_date = None, None, None, None
    if on_time < required - TOLERANCE:
        cause_code, cause_source_id, cause_available_date, cause_baseline_date = _cause_from_pool(
            buckets, demand.need_date
        )

    return DemandCoverage(
        plan_id=demand.plan_id,
        plan_line_id=demand.plan_line_id,
        required_quantity=required,
        on_time_quantity=on_time,
        full_cover_date=full_cover_date,
        uncovered_quantity=uncovered,
        cause_code=cause_code,
        cause_source_id=cause_source_id,
        cause_available_date=cause_available_date,
        cause_baseline_date=cause_baseline_date,
        on_hand=on_hand,
        last_late_date=last_late_date,
    )


def _zero_if_close(value: float) -> float:
    return 0.0 if abs(value) < TOLERANCE else value


def _cause_from_pool(
    buckets: Sequence[_Bucket], need_date: date
) -> tuple[str, str | None, date | None, date | None]:
    candidates = sorted(
        (
            bucket.receipt
            for bucket in buckets
            if bucket.receipt is not None
            and bucket.receipt.available_date > need_date
            and bucket.receipt.baseline_available_date is not None
            and bucket.receipt.baseline_available_date <= need_date
        ),
        key=lambda receipt: (receipt.available_date, receipt.source_id),
    )
    if not candidates:
        return DEMAND_EXCEEDS_SUPPLY, None, None, None
    winner = candidates[0]
    return (
        winner.reason_code or SUPPLY_DELAYED,
        winner.source_id,
        winner.available_date,
        winner.baseline_available_date,
    )


def _group_by_plan(coverages: Sequence[DemandCoverage]) -> dict[str, list[DemandCoverage]]:
    grouped: dict[str, list[DemandCoverage]] = {}
    for coverage in coverages:
        grouped.setdefault(coverage.plan_id, []).append(coverage)
    return grouped


def _build_plan_outcome(plan_id: str, lines: Sequence[DemandCoverage]) -> PlanSupplyOutcome:
    total_on_time = sum(line.on_time_quantity for line in lines)
    total_required = sum(line.required_quantity for line in lines)
    on_hand = lines[0].on_hand
    full_cover_dates = [line.full_cover_date for line in lines]
    all_have_full_cover = all(d is not None for d in full_cover_dates)
    known_full_cover_dates = [d for d in full_cover_dates if d is not None]
    max_full_cover = max(known_full_cover_dates) if known_full_cover_dates else None
    plan_full_cover_date = max_full_cover if all_have_full_cover else None

    if abs(total_on_time - total_required) < TOLERANCE:
        return PlanSupplyOutcome(
            plan_id=plan_id,
            shortfall_quantity=0.0,
            material_available_not_before=None,
            cause_code=None,
            cause_source_id=None,
            full_cover_date=plan_full_cover_date,
            required_quantity=total_required,
            on_time_quantity=total_on_time,
            on_hand=on_hand,
        )

    cause_code, cause_source_id, cause_available_date, cause_baseline_date = _select_cause(lines)

    if total_on_time > TOLERANCE:
        return PlanSupplyOutcome(
            plan_id=plan_id,
            shortfall_quantity=total_required - total_on_time,
            material_available_not_before=None,
            cause_code=cause_code,
            cause_source_id=cause_source_id,
            full_cover_date=plan_full_cover_date,
            required_quantity=total_required,
            on_time_quantity=total_on_time,
            on_hand=on_hand,
            cause_available_date=cause_available_date,
            cause_baseline_date=cause_baseline_date,
        )

    if all_have_full_cover:
        return PlanSupplyOutcome(
            plan_id=plan_id,
            shortfall_quantity=0.0,
            material_available_not_before=max_full_cover,
            cause_code=cause_code,
            cause_source_id=cause_source_id,
            full_cover_date=plan_full_cover_date,
            required_quantity=total_required,
            on_time_quantity=total_on_time,
            on_hand=on_hand,
            cause_available_date=cause_available_date,
            cause_baseline_date=cause_baseline_date,
        )

    # Nothing arrives on time and some quantity is never covered. What does arrive late still
    # ships late, so the plan is pushed to the latest late arrival it draws on; only the
    # never-covered quantity is SHORT. (Without this, partly covered lines would drop their
    # late units from both the LATE and the SHORT risk.)
    covered_dates = [line.last_late_date for line in lines if line.last_late_date is not None]
    return PlanSupplyOutcome(
        plan_id=plan_id,
        shortfall_quantity=sum(line.uncovered_quantity for line in lines),
        material_available_not_before=max(covered_dates) if covered_dates else None,
        cause_code=cause_code,
        cause_source_id=cause_source_id,
        full_cover_date=plan_full_cover_date,
        required_quantity=total_required,
        on_time_quantity=total_on_time,
        on_hand=on_hand,
        cause_available_date=cause_available_date,
        cause_baseline_date=cause_baseline_date,
    )


def _select_cause(lines: Sequence[DemandCoverage]) -> tuple[str | None, str | None, date | None, date | None]:
    worst = min(
        lines, key=lambda line: (-round(line.required_quantity - line.on_time_quantity, 6), line.plan_line_id)
    )
    return worst.cause_code, worst.cause_source_id, worst.cause_available_date, worst.cause_baseline_date
