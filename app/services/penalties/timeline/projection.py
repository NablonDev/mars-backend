"""Forward projection of a fulfillment plan's milestone timeline.

Pure, framework-free calculation: given the milestone reference definitions
(the `milestone_type` rows) and one plan's dated milestones and window
constraints, projects every applicable milestone's date, the retailer-window
timing risks that follow, and which milestone is driving a slip.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from app.services.penalties.timeline.types import (
    MilestoneDefinition,
    MilestoneState,
    PlanTimelineInput,
    PlanTimelineResult,
    ProjectedMilestone,
    TimingRisk,
)


def applicable_definitions(
    definitions: Sequence[MilestoneDefinition], freight_term: str
) -> list[MilestoneDefinition]:
    """Definitions in scope for `freight_term` (its own scope or ANY), by sequence."""
    return sorted(
        (d for d in definitions if d.freight_term_scope in ("ANY", freight_term)),
        key=lambda d: d.sequence_no,
    )


def project_timeline(
    definitions: Sequence[MilestoneDefinition], plan: PlanTimelineInput
) -> PlanTimelineResult:
    """Project every applicable milestone's date and the resulting timing risks.

    Raises `ValueError` when `plan.freight_term` does not resolve to exactly
    one applicable measurement-point milestone.
    """
    applicable = applicable_definitions(definitions, plan.freight_term)
    applicable_codes = {d.code for d in applicable}
    states_by_code = {m.code: m for m in plan.milestones}

    projected_by_code: dict[str, date] = {}
    for definition in applicable:
        projected_by_code[definition.code] = _project_milestone_date(
            definition, plan, applicable_codes, states_by_code, projected_by_code
        )

    milestones_out: list[ProjectedMilestone] = []
    slip_by_code: dict[str, int] = {}
    for definition in applicable:
        state = states_by_code.get(definition.code)
        baseline = state.baseline_date if state else None
        planned = state.planned_date if state else None
        actual = state.actual_date if state else None
        projected_date = projected_by_code[definition.code]
        reference = baseline if baseline is not None else planned
        slip_days = (projected_date - reference).days if reference is not None else 0
        slip_by_code[definition.code] = slip_days
        milestones_out.append(
            ProjectedMilestone(
                code=definition.code,
                baseline_date=baseline,
                planned_date=planned,
                projected_date=projected_date,
                actual_date=actual,
                slip_days=slip_days,
            )
        )

    measured_definitions = [d for d in applicable if d.is_measurement_point]
    if len(measured_definitions) != 1:
        raise ValueError(
            f"Plan {plan.plan_id!r} (freight_term={plan.freight_term!r}) resolves to "
            f"{len(measured_definitions)} applicable measurement-point milestones; expected exactly 1."
        )
    measured_code = measured_definitions[0].code
    measured_date = projected_by_code[measured_code]
    slack_days = (plan.window_end - measured_date).days

    driver_positive = find_driver(applicable, slip_by_code, negative=False)
    driver_negative = find_driver(applicable, slip_by_code, negative=True)
    timing_risks = _build_timing_risks(
        plan,
        applicable_codes,
        states_by_code,
        projected_by_code,
        measured_code,
        measured_date,
        driver_positive,
        driver_negative,
    )

    slipping = any(slip > 0 for slip in slip_by_code.values())
    slip_driver_code = driver_positive if slipping else None

    return PlanTimelineResult(
        plan_id=plan.plan_id,
        measured_milestone_code=measured_code,
        projected_measured_date=measured_date,
        slack_days=slack_days,
        milestones=tuple(milestones_out),
        timing_risks=tuple(timing_risks),
        slipping=slipping,
        slip_driver_code=slip_driver_code,
    )


def _project_milestone_date(
    definition: MilestoneDefinition,
    plan: PlanTimelineInput,
    applicable_codes: set[str],
    states_by_code: Mapping[str, MilestoneState],
    projected_by_code: Mapping[str, date],
) -> date:
    state = states_by_code.get(definition.code)
    if state is not None and state.actual_date is not None:
        return state.actual_date

    candidates = [plan.as_of]
    if state is not None:
        if state.planned_date is not None:
            candidates.append(state.planned_date)
        elif state.baseline_date is not None:
            candidates.append(state.baseline_date)

    not_before_date = plan.not_before.get(definition.code)
    if not_before_date is not None:
        candidates.append(not_before_date)

    duration = _duration_days(definition, plan)
    for dep_code in definition.depends_on:
        if dep_code not in applicable_codes:
            continue
        dep_projected = projected_by_code.get(dep_code)
        if dep_projected is not None:
            candidates.append(dep_projected + timedelta(days=duration))

    return max(candidates)


def milestone_duration_days(
    definition: MilestoneDefinition,
    planned_transit_days: int | None,
    duration_overrides: Mapping[str, float] | None = None,
    transit_override_days: int | None = None,
) -> int:
    """Duration in whole days for `definition`, honouring overrides and transit.

    A `duration_overrides[definition.code]` entry wins outright. Otherwise, a
    `default_duration_days is None` milestone (the freight-term transit leg)
    uses `transit_override_days` if given, else `planned_transit_days` (0 if
    unset); every other milestone uses its own `default_duration_days`.
    """
    overrides = duration_overrides or {}
    if definition.code in overrides:
        raw = overrides[definition.code]
    elif definition.default_duration_days is None:
        raw = transit_override_days if transit_override_days is not None else (planned_transit_days or 0)
    else:
        raw = definition.default_duration_days
    return math.ceil(raw)


def _duration_days(definition: MilestoneDefinition, plan: PlanTimelineInput) -> int:
    return milestone_duration_days(
        definition, plan.planned_transit_days, plan.duration_overrides, plan.transit_override_days
    )


def find_driver(
    applicable: Sequence[MilestoneDefinition], slip_by_code: Mapping[str, int], *, negative: bool
) -> str | None:
    """Earliest (by sequence) milestone that is the origin of a slip, or None.

    For `negative=False`: the earliest milestone whose own slip exceeds the
    largest slip among its applicable dependencies and is positive. For
    `negative=True`: the earliest milestone whose own slip is below the
    smallest slip among its applicable dependencies and is negative.
    """
    for definition in applicable:
        own_slip = slip_by_code[definition.code]
        dep_slips = [slip_by_code[dep] for dep in definition.depends_on if dep in slip_by_code]
        if negative:
            baseline_slip = min(dep_slips) if dep_slips else 0
            if own_slip < 0 and own_slip < baseline_slip:
                return definition.code
        else:
            baseline_slip = max(dep_slips) if dep_slips else 0
            if own_slip > 0 and own_slip > baseline_slip:
                return definition.code
    return None


def _build_timing_risks(
    plan: PlanTimelineInput,
    applicable_codes: set[str],
    states_by_code: Mapping[str, MilestoneState],
    projected_by_code: Mapping[str, date],
    measured_code: str,
    measured_date: date,
    driver_positive: str | None,
    driver_negative: str | None,
) -> list[TimingRisk]:
    risks: list[TimingRisk] = []
    measured_state = states_by_code.get(measured_code)
    measured_has_actual = measured_state is not None and measured_state.actual_date is not None

    not_delivered_emitted = False
    if plan.cancel_date is not None and measured_date > plan.cancel_date:
        status = "BREACHED" if measured_has_actual or plan.as_of > plan.cancel_date else "PROJECTED_BREACH"
        risks.append(
            TimingRisk(
                risk_type="NOT_DELIVERED",
                status=status,
                days_off=(measured_date - plan.cancel_date).days,
                driver_milestone_code=driver_positive,
            )
        )
        not_delivered_emitted = True

    if not not_delivered_emitted and measured_date > plan.window_end:
        status = "BREACHED" if measured_has_actual else "PROJECTED_BREACH"
        risks.append(
            TimingRisk(
                risk_type="LATE",
                status=status,
                days_off=(measured_date - plan.window_end).days,
                driver_milestone_code=driver_positive,
            )
        )

    if measured_date < plan.window_start:
        status = "BREACHED" if measured_has_actual else "PROJECTED_BREACH"
        risks.append(
            TimingRisk(
                risk_type="EARLY",
                status=status,
                days_off=(measured_date - plan.window_start).days,
                driver_milestone_code=driver_negative,
            )
        )

    if "ASN_SENT" in applicable_codes and "GOODS_ISSUED" in applicable_codes:
        asn_date = projected_by_code["ASN_SENT"]
        goods_issued_date = projected_by_code["GOODS_ISSUED"]
        if asn_date > goods_issued_date:
            asn_state = states_by_code.get("ASN_SENT")
            asn_has_actual = asn_state is not None and asn_state.actual_date is not None
            risks.append(
                TimingRisk(
                    risk_type="ASN_LATE",
                    status="BREACHED" if asn_has_actual else "PROJECTED_BREACH",
                    days_off=(asn_date - goods_issued_date).days,
                    driver_milestone_code="ASN_SENT",
                )
            )

    return risks
