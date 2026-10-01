"""Tests for the pure plan-assessment adapter (RED before assessment.py existed)."""

from dataclasses import replace
from datetime import date

import pytest

from app.services.penalties.projection import CalcType, PenaltyRule
from app.services.penalties.timeline.assessment import assess_plan, stack_amounts
from app.services.penalties.timeline.pricing import PricingBasis
from app.services.penalties.timeline.types import MilestoneState, PlanTimelineInput

from ._definitions import definitions

BASIS = PricingBasis(quantity=100.0, unit_cost=10.0, unit_price=20.0)


def _rule(rule_id: str, violation_type: str, rate: float = 1.0) -> PenaltyRule:
    return PenaltyRule(rule_id=rule_id, violation_type=violation_type, calc_type=CalcType.PER_UNIT, rate=rate)


def _plan(as_of: date, window_end: date, milestones=()) -> PlanTimelineInput:
    return PlanTimelineInput(
        plan_id="PLAN-1",
        freight_term="PREPAID",
        planned_transit_days=2,
        window_start=date(2026, 1, 1),
        window_end=window_end,
        cancel_date=None,
        milestones=milestones,
        as_of=as_of,
    )


def test_assess_plan_prices_a_late_projected_breach():
    plan = _plan(as_of=date(2026, 1, 10), window_end=date(2026, 1, 5))
    rule = _rule("R1", "OTIF_LATE", rate=2.0)

    assessment = assess_plan(definitions(), plan, 0.0, None, BASIS, [rule], "SUM")

    assert len(assessment.priced_risks) == 1
    priced = assessment.priced_risks[0]
    assert priced.risk_type == "LATE"
    assert priced.status == "PROJECTED_BREACH"
    assert priced.penalty_amount == pytest.approx(200.0)


def test_assess_plan_prices_short_alongside_timing_risk():
    plan = _plan(as_of=date(2026, 1, 3), window_end=date(2026, 1, 20))
    rule = _rule("R-SHORT", "SHORT_SHIP", rate=1.5)

    assessment = assess_plan(definitions(), plan, 10.0, "PROJECTED_BREACH", BASIS, [rule], "SUM")

    risk_types = {r.risk_type for r in assessment.priced_risks}
    assert "SHORT" in risk_types
    short = next(r for r in assessment.priced_risks if r.risk_type == "SHORT")
    assert short.penalty_amount == pytest.approx(15.0)


def test_assess_plan_does_not_price_short_on_top_of_not_delivered():
    # Past the cancel date the whole plan is NOT_DELIVERED (full quantity through the shortage
    # rules); a SHORT on the same goods would charge the retailer's shortage rule twice.
    plan = replace(_plan(as_of=date(2026, 1, 10), window_end=date(2026, 1, 5)), cancel_date=date(2026, 1, 7))
    rule = _rule("R-SHORT", "SHORT_SHIP", rate=1.5)

    assessment = assess_plan(definitions(), plan, 10.0, "PROJECTED_BREACH", BASIS, [rule], "SUM")

    risk_types = [r.risk_type for r in assessment.priced_risks]
    assert risk_types == ["NOT_DELIVERED"]
    assert assessment.priced_risks[0].penalty_amount == pytest.approx(150.0)  # 100 units x 1.5


def test_assess_plan_reports_no_risks_when_on_track():
    plan = _plan(as_of=date(2026, 1, 3), window_end=date(2026, 1, 20))

    assessment = assess_plan(definitions(), plan, 0.0, None, BASIS, [], "SUM")

    assert assessment.priced_risks == ()
    assert assessment.result.slipping is False


def test_assess_plan_with_a_done_milestone_reuses_actual_date():
    milestones = (
        MilestoneState(
            code="ORDER_RECEIVED", baseline_date=None, planned_date=None, actual_date=date(2026, 1, 1)
        ),
    )
    plan = _plan(as_of=date(2026, 1, 3), window_end=date(2026, 1, 20), milestones=milestones)

    assessment = assess_plan(definitions(), plan, 0.0, None, BASIS, [], "SUM")

    order_received = next(m for m in assessment.result.milestones if m.code == "ORDER_RECEIVED")
    assert order_received.projected_date == date(2026, 1, 1)


def test_stack_amounts_sums_by_default():
    assert stack_amounts([10.0, 20.0], "SUM") == 30.0


def test_stack_amounts_max_mode():
    assert stack_amounts([10.0, 20.0], "MAX") == 20.0


def test_stack_amounts_empty_is_zero():
    assert stack_amounts([], "SUM") == 0.0


def test_stack_amounts_rejects_unknown_mode():
    with pytest.raises(ValueError):
        stack_amounts([1.0], "BOGUS")
