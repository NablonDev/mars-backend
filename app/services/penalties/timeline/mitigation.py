"""Mitigation catalog and evaluator for a fulfillment plan's projected breach.

Pure, framework-free calculation: given a `PlanSituation` describing one plan's
projected breach and its root cause (driver milestone + reason), matches
candidate fixes from a fixed `CATALOG`, filters them to feasible ones, and
evaluates each by re-running the projection with the fix applied through an
injected `Repricer` callback. ACCEPT is always a candidate when any risk type
is present; every other option is either priced or reported infeasible with a
reason, never silently dropped.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from app.services.penalties.timeline.types import (
    MilestoneDefinition,
    PlanTimelineInput,
    PlanTimelineResult,
)

ROOT_CAUSE_MISMATCH = "ROOT_CAUSE_MISMATCH"
MILESTONE_ALREADY_DONE = "MILESTONE_ALREADY_DONE"
PREREQUISITE_NOT_DONE = "PREREQUISITE_NOT_DONE"
ACT_BY_PASSED = "ACT_BY_PASSED"
NOT_APPLICABLE = "NOT_APPLICABLE"

ALL_RISK_TYPES = frozenset({"LATE", "EARLY", "NOT_DELIVERED", "SHORT", "ASN_LATE"})

_WAIT_FOR_FULL_QUANTITY = "WAIT_FOR_FULL_QUANTITY"
_REQUEST_DATE_CHANGE = "REQUEST_DATE_CHANGE"
_ACCEPT = "ACCEPT"


@dataclass(frozen=True)
class MitigationAction:
    code: str
    owner_team: str
    addresses: frozenset[str]
    driver_codes: frozenset[str] | None
    driver_reasons: frozenset[str] | None
    freight_terms: frozenset[str]
    latest_milestone: str | None
    requires_done_milestone: str | None
    lead_days: int
    fixed_cost: float
    confidence: str


@dataclass(frozen=True)
class PlanSituation:
    plan: PlanTimelineInput
    definitions: tuple[MilestoneDefinition, ...]
    baseline_result: PlanTimelineResult
    risk_types: frozenset[str]
    driver_code: str | None
    driver_reason: str | None
    penalty_before: float
    shortfall_quantity: float
    wait_not_before: date | None = None


@dataclass(frozen=True)
class EvaluatedOption:
    action_code: str
    owner_team: str
    feasible: bool
    infeasible_reason: str | None
    act_by_date: date | None
    penalty_before: float
    penalty_after: float | None
    action_cost: float
    net_saving: float | None
    confidence: str
    addresses_risk_types: tuple[str, ...]
    rank_no: int | None
    rationale: str


Repricer = Callable[[PlanTimelineInput, float], float]


CATALOG: tuple[MitigationAction, ...] = (
    MitigationAction(
        code="PRIORITIZE_PICK",
        owner_team="Warehouse Ops",
        addresses=frozenset({"LATE", "NOT_DELIVERED"}),
        driver_codes=frozenset({"DELIVERY_CREATED", "PICKED", "LOADED", "READY_FOR_PICKUP"}),
        driver_reasons=None,
        freight_terms=frozenset({"PREPAID", "COLLECT"}),
        latest_milestone="PICKED",
        requires_done_milestone=None,
        lead_days=0,
        fixed_cost=300.0,
        confidence="CONFIRMED",
    ),
    MitigationAction(
        code="REBOOK_CARRIER",
        owner_team="Transportation",
        addresses=frozenset({"LATE", "NOT_DELIVERED"}),
        driver_codes=frozenset({"TENDER_ACCEPTED"}),
        driver_reasons=None,
        freight_terms=frozenset({"PREPAID"}),
        latest_milestone="TENDER_ACCEPTED",
        requires_done_milestone=None,
        lead_days=0,
        fixed_cost=450.0,
        confidence="CONFIRMED",
    ),
    MitigationAction(
        code="EXPEDITE_FREIGHT",
        owner_team="Transportation",
        addresses=frozenset({"LATE", "NOT_DELIVERED"}),
        driver_codes=None,
        driver_reasons=None,
        freight_terms=frozenset({"PREPAID"}),
        latest_milestone="GOODS_ISSUED",
        requires_done_milestone=None,
        lead_days=1,
        fixed_cost=900.0,
        confidence="CONFIRMED",
    ),
    MitigationAction(
        code="TEAM_DRIVERS",
        owner_team="Transportation",
        addresses=frozenset({"LATE", "NOT_DELIVERED"}),
        driver_codes=None,
        driver_reasons=None,
        freight_terms=frozenset({"PREPAID"}),
        latest_milestone="GOODS_ISSUED",
        requires_done_milestone=None,
        lead_days=1,
        fixed_cost=2200.0,
        confidence="CONFIRMED",
    ),
    MitigationAction(
        code="EXPEDITE_QA_RELEASE",
        owner_team="Quality",
        addresses=frozenset({"LATE", "NOT_DELIVERED", "SHORT"}),
        driver_codes=frozenset({"MATERIAL_AVAILABLE"}),
        driver_reasons=frozenset({"QA_HOLD"}),
        freight_terms=frozenset({"PREPAID", "COLLECT"}),
        latest_milestone="MATERIAL_AVAILABLE",
        requires_done_milestone=None,
        lead_days=0,
        fixed_cost=600.0,
        confidence="ESTIMATED",
    ),
    MitigationAction(
        code="OVERTIME_PRODUCTION",
        owner_team="Supply Planning",
        addresses=frozenset({"LATE", "NOT_DELIVERED", "SHORT"}),
        driver_codes=frozenset({"MATERIAL_AVAILABLE"}),
        driver_reasons=frozenset({"PRODUCTION_DELAY"}),
        freight_terms=frozenset({"PREPAID", "COLLECT"}),
        latest_milestone="MATERIAL_AVAILABLE",
        requires_done_milestone=None,
        lead_days=1,
        fixed_cost=1500.0,
        confidence="ESTIMATED",
    ),
    MitigationAction(
        code=_WAIT_FOR_FULL_QUANTITY,
        owner_team="Customer Service",
        addresses=frozenset({"SHORT"}),
        driver_codes=None,
        driver_reasons=None,
        freight_terms=frozenset({"PREPAID", "COLLECT"}),
        latest_milestone="GOODS_ISSUED",
        requires_done_milestone=None,
        lead_days=0,
        fixed_cost=0.0,
        confidence="CONFIRMED",
    ),
    MitigationAction(
        code="HOLD_SHIPMENT",
        owner_team="Warehouse Ops",
        addresses=frozenset({"EARLY"}),
        driver_codes=None,
        driver_reasons=None,
        freight_terms=frozenset({"PREPAID", "COLLECT"}),
        latest_milestone="GOODS_ISSUED",
        requires_done_milestone=None,
        lead_days=0,
        fixed_cost=150.0,
        confidence="CONFIRMED",
    ),
    MitigationAction(
        code="CARRIER_YARD_HOLD",
        owner_team="Transportation",
        addresses=frozenset({"EARLY"}),
        driver_codes=None,
        driver_reasons=None,
        freight_terms=frozenset({"PREPAID"}),
        latest_milestone="DELIVERED",
        requires_done_milestone="GOODS_ISSUED",
        lead_days=0,
        fixed_cost=450.0,
        confidence="CONFIRMED",
    ),
    MitigationAction(
        code="REBOOK_APPOINTMENT",
        owner_team="Transportation",
        addresses=frozenset({"LATE", "EARLY"}),
        driver_codes=frozenset({"APPOINTMENT_CONFIRMED"}),
        driver_reasons=None,
        freight_terms=frozenset({"PREPAID"}),
        latest_milestone="APPOINTMENT_CONFIRMED",
        requires_done_milestone=None,
        lead_days=0,
        fixed_cost=0.0,
        confidence="ESTIMATED",
    ),
    MitigationAction(
        code=_REQUEST_DATE_CHANGE,
        owner_team="Customer Service",
        addresses=frozenset({"LATE"}),
        driver_codes=None,
        driver_reasons=None,
        freight_terms=frozenset({"PREPAID", "COLLECT"}),
        latest_milestone=None,
        requires_done_milestone=None,
        lead_days=0,
        fixed_cost=0.0,
        confidence="ESTIMATED",
    ),
    MitigationAction(
        code=_ACCEPT,
        owner_team="Customer Service",
        addresses=ALL_RISK_TYPES,
        driver_codes=None,
        driver_reasons=None,
        freight_terms=frozenset({"PREPAID", "COLLECT"}),
        latest_milestone=None,
        requires_done_milestone=None,
        lead_days=0,
        fixed_cost=0.0,
        confidence="CONFIRMED",
    ),
)


def evaluate_mitigations(
    situation: PlanSituation, reprice: Repricer, catalog: Sequence[MitigationAction] = CATALOG
) -> list[EvaluatedOption]:
    """Evaluate every catalog action against `situation`, feasible ones ranked first."""
    candidates = [
        action
        for action in catalog
        if action.addresses & situation.risk_types and situation.plan.freight_term in action.freight_terms
    ]
    evaluated = [_evaluate_one(action, situation, reprice) for action in candidates]

    feasible = sorted(
        (option for option in evaluated if option.feasible),
        key=lambda option: (-(option.net_saving or 0.0), option.action_cost, option.action_code),
    )
    infeasible = sorted(
        (option for option in evaluated if not option.feasible), key=lambda option: option.action_code
    )
    for rank_no, option in enumerate(feasible, start=1):
        feasible[rank_no - 1] = replace(option, rank_no=rank_no)
    return [*feasible, *infeasible]


def _evaluate_one(action: MitigationAction, situation: PlanSituation, reprice: Repricer) -> EvaluatedOption:
    infeasible_reason = _infeasible_reason(action, situation)
    act_by_date = _act_by_date(action, situation)
    if infeasible_reason is None and act_by_date is not None and act_by_date < situation.plan.as_of:
        infeasible_reason = ACT_BY_PASSED

    addresses_risk_types = tuple(sorted(action.addresses & situation.risk_types))
    if infeasible_reason is not None:
        return EvaluatedOption(
            action_code=action.code,
            owner_team=action.owner_team,
            feasible=False,
            infeasible_reason=infeasible_reason,
            act_by_date=act_by_date,
            penalty_before=round(situation.penalty_before, 2),
            penalty_after=None,
            action_cost=action.fixed_cost,
            net_saving=None,
            confidence=action.confidence,
            addresses_risk_types=addresses_risk_types,
            rank_no=None,
            rationale=_rationale(action, situation, infeasible_reason, None),
        )

    penalty_after = _penalty_after(action, situation, reprice)
    net_saving = round(situation.penalty_before - penalty_after - action.fixed_cost, 2)
    return EvaluatedOption(
        action_code=action.code,
        owner_team=action.owner_team,
        feasible=True,
        infeasible_reason=None,
        act_by_date=act_by_date,
        penalty_before=round(situation.penalty_before, 2),
        penalty_after=round(penalty_after, 2),
        action_cost=action.fixed_cost,
        net_saving=net_saving,
        confidence=action.confidence,
        addresses_risk_types=addresses_risk_types,
        rank_no=None,
        rationale=_rationale(action, situation, None, penalty_after),
    )


def _infeasible_reason(action: MitigationAction, situation: PlanSituation) -> str | None:
    if action.driver_codes is not None and situation.driver_code not in action.driver_codes:
        return ROOT_CAUSE_MISMATCH
    if action.driver_reasons is not None and situation.driver_reason not in action.driver_reasons:
        return ROOT_CAUSE_MISMATCH
    if action.latest_milestone is not None and _milestone_done(situation.plan, action.latest_milestone):
        return MILESTONE_ALREADY_DONE
    if action.requires_done_milestone is not None and not _milestone_done(
        situation.plan, action.requires_done_milestone
    ):
        return PREREQUISITE_NOT_DONE
    if action.code == _WAIT_FOR_FULL_QUANTITY and situation.wait_not_before is None:
        return NOT_APPLICABLE
    return None


def _milestone_done(plan: PlanTimelineInput, code: str) -> bool:
    state = next((m for m in plan.milestones if m.code == code), None)
    return state is not None and state.actual_date is not None


def _act_by_date(action: MitigationAction, situation: PlanSituation) -> date | None:
    if action.code == _REQUEST_DATE_CHANGE:
        return situation.plan.window_start - timedelta(days=2)
    if action.latest_milestone is None:
        return None
    milestone = next(
        (m for m in situation.baseline_result.milestones if m.code == action.latest_milestone), None
    )
    if milestone is None:
        return None
    return milestone.projected_date - timedelta(days=action.lead_days)


def _penalty_after(action: MitigationAction, situation: PlanSituation, reprice: Repricer) -> float:
    if action.code == _REQUEST_DATE_CHANGE:
        return round(situation.penalty_before * 0.5, 2)
    if action.code == _ACCEPT:
        return situation.penalty_before
    modified_plan, modified_shortfall = _apply_action(action, situation)
    return reprice(modified_plan, modified_shortfall)


def _apply_action(action: MitigationAction, situation: PlanSituation) -> tuple[PlanTimelineInput, float]:
    plan = situation.plan
    if action.code == "PRIORITIZE_PICK":
        overrides = {**plan.duration_overrides, "PICKED": 0}
        modified_plan = _replan_milestone_now(replace(plan, duration_overrides=overrides), "PICKED")
        return modified_plan, situation.shortfall_quantity
    if action.code == "REBOOK_CARRIER":
        overrides = {**plan.duration_overrides, "TENDER_ACCEPTED": 0}
        not_before = {k: v for k, v in plan.not_before.items() if k != "TENDER_ACCEPTED"}
        modified_plan = replace(plan, duration_overrides=overrides, not_before=not_before)
        modified_plan = _replan_milestone_now(modified_plan, "TENDER_ACCEPTED")
        return modified_plan, situation.shortfall_quantity
    if action.code == "EXPEDITE_FREIGHT":
        new_transit = max(1, _current_transit(plan) - 1)
        return replace(plan, transit_override_days=new_transit), situation.shortfall_quantity
    if action.code == "TEAM_DRIVERS":
        new_transit = max(1, math.ceil(_current_transit(plan) / 2))
        return replace(plan, transit_override_days=new_transit), situation.shortfall_quantity
    if action.code in ("EXPEDITE_QA_RELEASE", "OVERTIME_PRODUCTION"):
        return _apply_pull_material_available(plan, situation)
    if action.code == _WAIT_FOR_FULL_QUANTITY:
        assert situation.wait_not_before is not None  # feasibility already guarded this
        not_before = {**plan.not_before, "MATERIAL_AVAILABLE": situation.wait_not_before}
        return replace(plan, not_before=not_before), 0.0
    if action.code == "HOLD_SHIPMENT":
        return _apply_hold_shipment(plan), situation.shortfall_quantity
    if action.code == "CARRIER_YARD_HOLD":
        not_before = {**plan.not_before, "DELIVERED": plan.window_start}
        return replace(plan, not_before=not_before), situation.shortfall_quantity
    if action.code == "REBOOK_APPOINTMENT":
        overrides = {**plan.duration_overrides, "APPOINTMENT_CONFIRMED": 0}
        modified_plan = _replan_milestone_now(
            replace(plan, duration_overrides=overrides), "APPOINTMENT_CONFIRMED"
        )
        return modified_plan, situation.shortfall_quantity
    raise ValueError(f"Unrecognized mitigation action code={action.code!r}")


def _replan_milestone_now(plan: PlanTimelineInput, code: str) -> PlanTimelineInput:
    """Pull one pending milestone's `planned_date` to `plan.as_of`: "do this step now".

    Leaves `baseline_date` untouched and does nothing to a milestone that has
    already completed (`actual_date` set) or has no recorded state at all --
    a duration override alone can't unstick a milestone that was hard-replanned
    to a later date, since the projection floors a pending milestone at its
    own `planned_date` regardless of how short its duration is (see
    PRIORITIZE_PICK/REBOOK_CARRIER/REBOOK_APPOINTMENT above).
    """
    milestones = tuple(
        replace(m, planned_date=plan.as_of) if m.code == code and m.actual_date is None else m
        for m in plan.milestones
    )
    return replace(plan, milestones=milestones)


def _current_transit(plan: PlanTimelineInput) -> int:
    if plan.transit_override_days is not None:
        return plan.transit_override_days
    return plan.planned_transit_days or 0


def _apply_pull_material_available(
    plan: PlanTimelineInput, situation: PlanSituation
) -> tuple[PlanTimelineInput, float]:
    not_before = dict(plan.not_before)
    current = plan.not_before.get("MATERIAL_AVAILABLE")
    if current is not None:
        not_before["MATERIAL_AVAILABLE"] = max(current - timedelta(days=2), plan.as_of)
    modified_shortfall = 0.0 if "SHORT" in situation.risk_types else situation.shortfall_quantity
    return replace(plan, not_before=not_before), modified_shortfall


def _apply_hold_shipment(plan: PlanTimelineInput) -> PlanTimelineInput:
    not_before = dict(plan.not_before)
    if plan.freight_term == "PREPAID":
        not_before["GOODS_ISSUED"] = plan.window_start - timedelta(days=_current_transit(plan))
    else:
        not_before["READY_FOR_PICKUP"] = plan.window_start
    return replace(plan, not_before=not_before)


def _rationale(
    action: MitigationAction,
    situation: PlanSituation,
    infeasible_reason: str | None,
    penalty_after: float | None,
) -> str:
    if infeasible_reason is not None:
        reason_text = infeasible_reason.replace("_", " ").lower()
        return f"{action.code}: infeasible ({reason_text})."
    detail = _rationale_detail(action, situation)
    penalty_before = situation.penalty_before
    after = penalty_after if penalty_after is not None else penalty_before
    return f"{detail}; projected penalty {penalty_before:.2f} -> {after:.2f} for {action.fixed_cost:.2f}."


def _rationale_detail(action: MitigationAction, situation: PlanSituation) -> str:
    plan = situation.plan
    if action.code == "PRIORITIZE_PICK":
        return "Cuts PICKED lead time to 0 days"
    if action.code == "REBOOK_CARRIER":
        return "Rebooks TENDER_ACCEPTED with 0-day turnaround"
    if action.code == "EXPEDITE_FREIGHT":
        transit = _current_transit(plan)
        return f"Cuts transit from {transit} to {max(1, transit - 1)} days"
    if action.code == "TEAM_DRIVERS":
        transit = _current_transit(plan)
        return f"Cuts transit from {transit} to {max(1, math.ceil(transit / 2))} days"
    if action.code in ("EXPEDITE_QA_RELEASE", "OVERTIME_PRODUCTION"):
        return "Pulls MATERIAL_AVAILABLE forward by 2 days"
    if action.code == _WAIT_FOR_FULL_QUANTITY:
        return "Waits for full quantity coverage"
    if action.code == "HOLD_SHIPMENT":
        return "Holds shipment until the delivery window opens"
    if action.code == "CARRIER_YARD_HOLD":
        return "Holds delivery in the carrier yard until the window opens"
    if action.code == "REBOOK_APPOINTMENT":
        return "Rebooks the delivery appointment with 0-day lead"
    if action.code == _REQUEST_DATE_CHANGE:
        return "Requests a retailer date change (acceptance not guaranteed)"
    return "Accepts the projected penalty with no mitigation"
