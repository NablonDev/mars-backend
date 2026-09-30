"""SAP-style backward scheduling of a fulfillment plan's baseline milestones.

Pure, framework-free calculation: given the milestone reference definitions
(the `milestone_type` rows), a freight term, the order date, and the
retailer's delivery window, computes every applicable milestone's baseline
date by scheduling backward from the measurement point's target date, then
forward from the order date to keep every dependency feasible.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from app.services.penalties.timeline.projection import applicable_definitions, milestone_duration_days
from app.services.penalties.timeline.types import MilestoneDefinition

ORDER_RECEIVED = "ORDER_RECEIVED"
ORDER_CONFIRMED = "ORDER_CONFIRMED"


def target_measured_date(window_start: date, window_end: date) -> date:
    """The delivery window's midpoint, the measured milestone's target date."""
    return window_start + timedelta(days=(window_end - window_start).days // 2)


def backward_schedule(
    definitions: Sequence[MilestoneDefinition],
    freight_term: str,
    order_date: date,
    window_start: date,
    window_end: date,
    planned_transit_days: int,
) -> dict[str, date]:
    """Baseline date for every milestone applicable to `freight_term`.

    `ORDER_RECEIVED`/`ORDER_CONFIRMED` are anchored forward from `order_date`.
    The measured milestone is anchored at `target_measured_date`, its
    ancestors are scheduled backward from it, and every other applicable
    milestone is scheduled forward from its dependencies. A final feasibility
    pass (ascending sequence) pushes any milestone forward that a tight
    delivery window scheduled earlier than its own dependencies allow.
    """
    applicable = applicable_definitions(definitions, freight_term)
    by_code = {d.code: d for d in applicable}
    successors_of = _successors(applicable)

    def duration(code: str) -> int:
        return milestone_duration_days(by_code[code], planned_transit_days)

    dates: dict[str, date] = {ORDER_RECEIVED: order_date}
    dates[ORDER_CONFIRMED] = order_date + timedelta(days=duration(ORDER_CONFIRMED))

    measured_code = next(d.code for d in applicable if d.is_measurement_point)
    dates[measured_code] = target_measured_date(window_start, window_end)

    ancestor_codes = _ancestors(by_code, measured_code) - {ORDER_RECEIVED, ORDER_CONFIRMED}
    for code in sorted(ancestor_codes, key=lambda c: by_code[c].sequence_no, reverse=True):
        scheduled_successors = [s for s in successors_of[code] if s in dates]
        dates[code] = min(dates[s] - timedelta(days=duration(s)) for s in scheduled_successors)

    unscheduled_codes = [d.code for d in applicable if d.code not in dates]
    for code in sorted(unscheduled_codes, key=lambda c: by_code[c].sequence_no):
        scheduled_deps = [dep for dep in by_code[code].depends_on if dep in dates]
        dates[code] = max(dates[dep] + timedelta(days=duration(code)) for dep in scheduled_deps)

    feasibility_codes = [c for c in dates if c not in (ORDER_RECEIVED, ORDER_CONFIRMED)]
    for code in sorted(feasibility_codes, key=lambda c: by_code[c].sequence_no):
        scheduled_deps = [dep for dep in by_code[code].depends_on if dep in dates]
        if not scheduled_deps:
            continue
        feasible_date = max(dates[dep] + timedelta(days=duration(code)) for dep in scheduled_deps)
        dates[code] = max(dates[code], feasible_date)

    return dates


def _successors(applicable: Sequence[MilestoneDefinition]) -> dict[str, list[str]]:
    successors_of: dict[str, list[str]] = {d.code: [] for d in applicable}
    for definition in applicable:
        for dep_code in definition.depends_on:
            if dep_code in successors_of:
                successors_of[dep_code].append(definition.code)
    return successors_of


def _ancestors(by_code: Mapping[str, MilestoneDefinition], code: str) -> set[str]:
    ancestors: set[str] = set()
    pending = list(by_code[code].depends_on)
    while pending:
        candidate = pending.pop()
        if candidate not in by_code or candidate in ancestors:
            continue
        ancestors.add(candidate)
        pending.extend(by_code[candidate].depends_on)
    return ancestors
