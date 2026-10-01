"""Tests for the pure mitigation catalog and evaluator."""

from datetime import date, timedelta

from app.services.penalties.projection import CalcType, PenaltyRule
from app.services.penalties.timeline.mitigation import (
    ACT_BY_PASSED,
    MILESTONE_ALREADY_DONE,
    NOT_APPLICABLE,
    PREREQUISITE_NOT_DONE,
    ROOT_CAUSE_MISMATCH,
    PlanSituation,
    evaluate_mitigations,
)
from app.services.penalties.timeline.pricing import PricingBasis, price_risk
from app.services.penalties.timeline.projection import project_timeline
from app.services.penalties.timeline.types import MilestoneState, PlanTimelineInput
from tests.unit.services.penalties.timeline._definitions import definitions as _definitions

DAY0 = date(2026, 1, 1)


def _state(code: str, *, actual: date | None = None, planned: date | None = None) -> MilestoneState:
    return MilestoneState(code=code, baseline_date=None, planned_date=planned, actual_date=actual)


def _plan(
    *,
    freight_term: str = "PREPAID",
    planned_transit_days: int | None = 2,
    window_start: date = DAY0,
    window_end: date = DAY0 + timedelta(days=30),
    cancel_date: date | None = None,
    milestones: tuple[MilestoneState, ...] = (),
    as_of: date = DAY0,
    not_before: dict[str, date] | None = None,
    duration_overrides: dict[str, float] | None = None,
    transit_override_days: int | None = None,
) -> PlanTimelineInput:
    return PlanTimelineInput(
        plan_id="PLAN-1",
        freight_term=freight_term,
        planned_transit_days=planned_transit_days,
        window_start=window_start,
        window_end=window_end,
        cancel_date=cancel_date,
        milestones=milestones,
        as_of=as_of,
        not_before=not_before or {},
        duration_overrides=duration_overrides or {},
        transit_override_days=transit_override_days,
    )


def _situation(
    *,
    plan: PlanTimelineInput,
    risk_types: set[str],
    driver_code: str | None = None,
    driver_reason: str | None = None,
    penalty_before: float = 1000.0,
    shortfall_quantity: float = 0.0,
    wait_not_before: date | None = None,
) -> PlanSituation:
    definitions = _definitions()
    baseline_result = project_timeline(definitions, plan)
    return PlanSituation(
        plan=plan,
        definitions=tuple(definitions),
        baseline_result=baseline_result,
        risk_types=frozenset(risk_types),
        driver_code=driver_code,
        driver_reason=driver_reason,
        penalty_before=penalty_before,
        shortfall_quantity=shortfall_quantity,
        wait_not_before=wait_not_before,
    )


def _find(options, code):
    return next(o for o in options if o.action_code == code)


def _zero_reprice(_plan: PlanTimelineInput, _shortfall: float) -> float:
    return 0.0


def test_root_cause_mismatch_blocks_expedite_qa_release_for_picked_driven_slip():
    plan = _plan()
    situation = _situation(plan=plan, risk_types={"LATE"}, driver_code="PICKED")

    options = evaluate_mitigations(situation, _zero_reprice)

    option = _find(options, "EXPEDITE_QA_RELEASE")
    assert option.feasible is False
    assert option.infeasible_reason == ROOT_CAUSE_MISMATCH


def test_expedite_freight_infeasible_once_goods_issued_is_done():
    plan = _plan(milestones=(_state("GOODS_ISSUED", actual=DAY0),))
    situation = _situation(plan=plan, risk_types={"LATE"})

    options = evaluate_mitigations(situation, _zero_reprice)

    option = _find(options, "EXPEDITE_FREIGHT")
    assert option.feasible is False
    assert option.infeasible_reason == MILESTONE_ALREADY_DONE


def test_carrier_yard_hold_requires_goods_issued_done():
    plan = _plan(milestones=())
    situation = _situation(plan=plan, risk_types={"EARLY"})

    options = evaluate_mitigations(situation, _zero_reprice)

    option = _find(options, "CARRIER_YARD_HOLD")
    assert option.feasible is False
    assert option.infeasible_reason == PREREQUISITE_NOT_DONE


def test_carrier_yard_hold_feasible_once_goods_issued_is_done():
    plan = _plan(milestones=(_state("GOODS_ISSUED", actual=DAY0),))
    situation = _situation(plan=plan, risk_types={"EARLY"})

    options = evaluate_mitigations(situation, _zero_reprice)

    option = _find(options, "CARRIER_YARD_HOLD")
    assert option.feasible is True
    assert option.infeasible_reason is None


def test_act_by_passed_when_window_start_leaves_no_lead_time():
    plan = _plan(window_start=DAY0, as_of=DAY0 + timedelta(days=5))
    situation = _situation(plan=plan, risk_types={"LATE"})

    options = evaluate_mitigations(situation, _zero_reprice)

    option = _find(options, "REQUEST_DATE_CHANGE")
    assert option.feasible is False
    assert option.infeasible_reason == ACT_BY_PASSED
    assert option.act_by_date == DAY0 - timedelta(days=2)


def test_wait_for_full_quantity_not_applicable_without_wait_not_before():
    plan = _plan()
    situation = _situation(plan=plan, risk_types={"SHORT"}, shortfall_quantity=5.0)

    options = evaluate_mitigations(situation, _zero_reprice)

    option = _find(options, "WAIT_FOR_FULL_QUANTITY")
    assert option.feasible is False
    assert option.infeasible_reason == NOT_APPLICABLE


def test_wait_for_full_quantity_feasible_and_zeroes_shortfall():
    calls = []

    def reprice(modified_plan: PlanTimelineInput, shortfall: float) -> float:
        calls.append((modified_plan, shortfall))
        return 0.0

    wait_date = DAY0 + timedelta(days=10)
    plan = _plan()
    situation = _situation(plan=plan, risk_types={"SHORT"}, shortfall_quantity=5.0, wait_not_before=wait_date)

    options = evaluate_mitigations(situation, reprice)

    option = _find(options, "WAIT_FOR_FULL_QUANTITY")
    assert option.feasible is True
    modified_plan, modified_shortfall = calls[0]
    assert modified_shortfall == 0.0
    assert modified_plan.not_before["MATERIAL_AVAILABLE"] == wait_date


def test_expedite_qa_release_zeroes_shortfall_when_short_is_a_risk_type():
    calls = []

    def reprice(modified_plan: PlanTimelineInput, shortfall: float) -> float:
        calls.append(shortfall)
        return 0.0

    plan = _plan(not_before={"MATERIAL_AVAILABLE": DAY0 + timedelta(days=10)})
    situation = _situation(
        plan=plan,
        risk_types={"SHORT"},
        driver_code="MATERIAL_AVAILABLE",
        driver_reason="QA_HOLD",
        shortfall_quantity=5.0,
    )

    options = evaluate_mitigations(situation, reprice)

    option = _find(options, "EXPEDITE_QA_RELEASE")
    assert option.feasible is True
    assert calls == [0.0]


def test_expedite_qa_release_keeps_shortfall_when_short_not_a_risk_type():
    calls = []

    def reprice(modified_plan: PlanTimelineInput, shortfall: float) -> float:
        calls.append(shortfall)
        return 0.0

    plan = _plan(
        not_before={"MATERIAL_AVAILABLE": DAY0 + timedelta(days=10)},
        milestones=(_state("GOODS_ISSUED", actual=DAY0),),
    )
    situation = _situation(
        plan=plan,
        risk_types={"LATE"},
        driver_code="MATERIAL_AVAILABLE",
        driver_reason="QA_HOLD",
        shortfall_quantity=5.0,
    )

    options = evaluate_mitigations(situation, reprice)

    option = _find(options, "EXPEDITE_QA_RELEASE")
    assert option.feasible is True
    assert calls == [5.0]


def test_expedite_qa_release_moves_not_before_earlier_but_not_before_as_of():
    calls = []

    def reprice(modified_plan: PlanTimelineInput, shortfall: float) -> float:
        calls.append(modified_plan)
        return 0.0

    plan = _plan(
        as_of=DAY0,
        not_before={"MATERIAL_AVAILABLE": DAY0 + timedelta(days=1)},
        milestones=(_state("GOODS_ISSUED", actual=DAY0),),
    )
    situation = _situation(
        plan=plan,
        risk_types={"LATE"},
        driver_code="MATERIAL_AVAILABLE",
        driver_reason="QA_HOLD",
    )

    evaluate_mitigations(situation, reprice)

    assert len(calls) == 1
    assert calls[0].not_before["MATERIAL_AVAILABLE"] == DAY0


def test_request_date_change_halves_the_penalty_without_calling_reprice():
    def raising_reprice(_plan: PlanTimelineInput, _shortfall: float) -> float:
        raise AssertionError("REQUEST_DATE_CHANGE must not call the repricer")

    plan = _plan(
        window_start=DAY0 + timedelta(days=30),
        window_end=DAY0 + timedelta(days=60),
        milestones=(_state("GOODS_ISSUED", actual=DAY0),),
    )
    situation = _situation(plan=plan, risk_types={"LATE"}, penalty_before=1000.0)

    options = evaluate_mitigations(situation, raising_reprice)

    option = _find(options, "REQUEST_DATE_CHANGE")
    assert option.feasible is True
    assert option.penalty_after == 500.0
    assert option.net_saving == 500.0


def test_ranking_orders_by_net_saving_with_accept_at_zero_and_negatives_below():
    def reprice(_plan: PlanTimelineInput, _shortfall: float) -> float:
        return 500.0

    plan = _plan(
        planned_transit_days=2,
        window_start=DAY0 + timedelta(days=30),
        window_end=DAY0 + timedelta(days=60),
    )
    situation = _situation(plan=plan, risk_types={"LATE"}, penalty_before=1000.0)

    options = evaluate_mitigations(situation, reprice)
    feasible = [o for o in options if o.feasible]
    ordered_codes = [o.action_code for o in feasible]

    assert ordered_codes == ["REQUEST_DATE_CHANGE", "ACCEPT", "EXPEDITE_FREIGHT", "TEAM_DRIVERS"]
    assert [o.net_saving for o in feasible] == [500.0, 0.0, -400.0, -1700.0]
    assert [o.rank_no for o in feasible] == [1, 2, 3, 4]
    accept = _find(options, "ACCEPT")
    assert accept.net_saving == 0.0
    assert accept.penalty_after == 1000.0


def test_collect_plan_never_offers_prepaid_only_actions():
    plan = _plan(freight_term="COLLECT")
    situation = _situation(plan=plan, risk_types={"LATE", "NOT_DELIVERED", "EARLY"})

    options = evaluate_mitigations(situation, _zero_reprice)

    codes = {o.action_code for o in options}
    prepaid_only_codes = {
        "REBOOK_CARRIER",
        "EXPEDITE_FREIGHT",
        "TEAM_DRIVERS",
        "CARRIER_YARD_HOLD",
        "REBOOK_APPOINTMENT",
    }
    assert codes.isdisjoint(prepaid_only_codes)


def test_infeasible_options_are_sorted_by_code_after_feasible_ones():
    plan = _plan(milestones=(_state("GOODS_ISSUED", actual=DAY0),))
    situation = _situation(plan=plan, risk_types={"LATE"})

    options = evaluate_mitigations(situation, _zero_reprice)

    infeasible_codes = [o.action_code for o in options if not o.feasible]
    assert infeasible_codes == sorted(infeasible_codes)
    assert all(o.rank_no is None for o in options if not o.feasible)


def test_real_wiring_with_project_timeline_and_price_risk():
    """Repricer built from the real projection + pricing modules, kept local to this test."""
    basis = PricingBasis(quantity=100.0, unit_cost=10.0, unit_price=20.0)
    rules = [PenaltyRule(rule_id="R1", violation_type="OTIF_LATE", calc_type=CalcType.PER_UNIT, rate=1.0)]
    definitions = _definitions()

    def real_reprice(modified_plan: PlanTimelineInput, shortfall: float) -> float:
        result = project_timeline(definitions, modified_plan)
        total = 0.0
        risk_types = {r.risk_type for r in result.timing_risks if r.status == "PROJECTED_BREACH"}
        if shortfall > 0:
            risk_types.add("SHORT")
        for risk in result.timing_risks:
            if risk.status != "PROJECTED_BREACH":
                continue
            priced = price_risk(risk.risk_type, risk.status, risk.days_off, None, basis, rules, "SUM")
            total += priced.penalty_amount
        if "SHORT" in risk_types and shortfall > 0:
            priced = price_risk("SHORT", "PROJECTED_BREACH", None, shortfall, basis, rules, "SUM")
            total += priced.penalty_amount
        return round(total, 2)

    plan = _plan(
        window_end=DAY0 + timedelta(days=5),
        not_before={"DELIVERY_CREATED": DAY0 + timedelta(days=3)},
    )
    baseline_penalty = real_reprice(plan, 0.0)
    assert baseline_penalty == 100.0  # 1 day late * rate(1) * qty(100)

    situation = _situation(
        plan=plan, risk_types={"LATE"}, driver_code="PICKED", penalty_before=baseline_penalty
    )

    options = evaluate_mitigations(situation, real_reprice)

    option = _find(options, "PRIORITIZE_PICK")
    assert option.feasible is True
    assert option.penalty_after == 0.0
    assert option.net_saving == -200.0  # 100 - 0 - 300 fixed cost


def _real_reprice(basis: PricingBasis, rules: list[PenaltyRule], definitions):
    def reprice(modified_plan: PlanTimelineInput, shortfall: float) -> float:
        result = project_timeline(definitions, modified_plan)
        total = 0.0
        for risk in result.timing_risks:
            if risk.status != "PROJECTED_BREACH":
                continue
            priced = price_risk(risk.risk_type, risk.status, risk.days_off, None, basis, rules, "SUM")
            total += priced.penalty_amount
        return round(total, 2)

    return reprice


def test_prioritize_pick_fixes_a_hard_replanned_picked_slip():
    """A milestone hard-replanned to a later `planned_date` (not just a slow duration) is a
    projection floor a duration override alone can't clear -- PRIORITIZE_PICK must also pull
    PICKED's own `planned_date` to `as_of` for the fix to actually reduce the penalty."""
    basis = PricingBasis(quantity=100.0, unit_cost=10.0, unit_price=20.0)
    rules = [PenaltyRule(rule_id="R1", violation_type="OTIF_LATE", calc_type=CalcType.PER_UNIT, rate=1.0)]
    definitions = _definitions()
    reprice = _real_reprice(basis, rules, definitions)

    plan = _plan(
        window_end=DAY0 + timedelta(days=5), milestones=(_state("PICKED", planned=DAY0 + timedelta(days=8)),)
    )
    baseline_penalty = reprice(plan, 0.0)
    assert baseline_penalty > 0

    situation = _situation(
        plan=plan, risk_types={"LATE"}, driver_code="PICKED", penalty_before=baseline_penalty
    )
    options = evaluate_mitigations(situation, reprice)

    option = _find(options, "PRIORITIZE_PICK")
    assert option.feasible is True
    assert option.penalty_after == 0.0
    assert option.penalty_after < baseline_penalty


def test_rebook_carrier_fixes_a_hard_replanned_tender_accepted_slip():
    basis = PricingBasis(quantity=100.0, unit_cost=10.0, unit_price=20.0)
    rules = [PenaltyRule(rule_id="R1", violation_type="OTIF_LATE", calc_type=CalcType.PER_UNIT, rate=1.0)]
    definitions = _definitions()
    reprice = _real_reprice(basis, rules, definitions)

    plan = _plan(
        window_end=DAY0 + timedelta(days=5),
        milestones=(_state("TENDER_ACCEPTED", planned=DAY0 + timedelta(days=8)),),
    )
    baseline_penalty = reprice(plan, 0.0)
    assert baseline_penalty > 0

    situation = _situation(
        plan=plan, risk_types={"LATE"}, driver_code="TENDER_ACCEPTED", penalty_before=baseline_penalty
    )
    options = evaluate_mitigations(situation, reprice)

    option = _find(options, "REBOOK_CARRIER")
    assert option.feasible is True
    assert option.penalty_after == 0.0
    assert option.penalty_after < baseline_penalty


def test_rebook_appointment_fixes_a_hard_replanned_appointment_confirmed_slip():
    basis = PricingBasis(quantity=100.0, unit_cost=10.0, unit_price=20.0)
    rules = [PenaltyRule(rule_id="R1", violation_type="OTIF_LATE", calc_type=CalcType.PER_UNIT, rate=1.0)]
    definitions = _definitions()
    reprice = _real_reprice(basis, rules, definitions)

    plan = _plan(
        window_end=DAY0 + timedelta(days=5),
        milestones=(_state("APPOINTMENT_CONFIRMED", planned=DAY0 + timedelta(days=8)),),
    )
    baseline_penalty = reprice(plan, 0.0)
    assert baseline_penalty > 0

    situation = _situation(
        plan=plan,
        risk_types={"LATE"},
        driver_code="APPOINTMENT_CONFIRMED",
        penalty_before=baseline_penalty,
    )
    options = evaluate_mitigations(situation, reprice)

    option = _find(options, "REBOOK_APPOINTMENT")
    assert option.feasible is True
    assert option.penalty_after == 0.0
    assert option.penalty_after < baseline_penalty
