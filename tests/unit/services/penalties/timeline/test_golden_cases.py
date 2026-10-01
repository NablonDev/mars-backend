"""Golden cases for the deterministic Penalty Intelligence engine (GC-01 .. GC-25).

Every case is hand-calculated in `docs/architecture/penalty-engine-golden-cases.md` (same GC-ID) and
pins one business assumption (D-numbers, `docs/architecture/penalty-engine-assumptions.md`). The suite
is pure: no DB, no clock. `D0` is a fixed calendar date and every other date is `D0 + n days`, written
`_d(n)`. The milestone table comes from `_definitions.py`, a copy of the 12 seed rows in
`scripts/seed/seed_milestone_types.py`.

Standard plan unless a test says otherwise: PREPAID, 2-day transit, delivery window `_d(3)`..`_d(7)`
(Mar 5..Mar 9), no cancel date, `as_of = D0`. With no slip, DELIVERED projects to `_d(5)` (Mar 7).
Standard pricing basis: 2,000 units, PO price 20.00, Mars standard cost 19.50 (cost differs from price
on purpose, so every penalty here also guards D5: penalties price off the PO unit price).

Behaviour these cases pin (all implemented):
  - D5 / B1: `pricing._unit_amount` returns `unit_price` regardless of `basis_type`.
  - B3: the dispute engine prices ASN_LATE from the ASN and goods-issue dates (GC-24).
  - B4: GC-17, late partial coverage carries a not-before date.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.penalties.dispute.engine import recompute_dispute
from app.services.penalties.dispute.types import DisputeFacts, DisputeVerdict
from app.services.penalties.projection import (
    APPLIES_PER_DAY,
    CalcType,
    PenaltyRule,
    PenaltyRuleTier,
)
from app.services.penalties.timeline.alerts import AlertState, CurrentRisk, plan_alert_changes
from app.services.penalties.timeline.assessment import PlanAssessment, assess_plan, stack_amounts
from app.services.penalties.timeline.mitigation import (
    ROOT_CAUSE_MISMATCH,
    PlanSituation,
    evaluate_mitigations,
)
from app.services.penalties.timeline.pricing import PricedRisk, PricingBasis, price_risk
from app.services.penalties.timeline.supply import (
    PlanSupplyOutcome,
    SupplyDemand,
    SupplyReceipt,
    allocate_supply,
    plan_outcomes,
)
from app.services.penalties.timeline.types import MilestoneState, PlanTimelineInput, TimingRisk
from tests.unit.services.penalties.timeline._definitions import definitions as _definitions

D0 = date(2026, 3, 2)
NOW = datetime(2026, 3, 13, 12, 0, tzinfo=UTC)

BASIS = PricingBasis(quantity=2000.0, unit_cost=19.50, unit_price=20.00)
BASIS_100 = PricingBasis(quantity=100.0, unit_cost=19.50, unit_price=20.00)

# Unshifted baseline schedule, in days after D0, for a plan that starts on D0 with no slip.
_PREPAID_BASELINE = {
    "ORDER_RECEIVED": 0,
    "ORDER_CONFIRMED": 1,
    "MATERIAL_AVAILABLE": 1,
    "TENDER_ACCEPTED": 2,
    "DELIVERY_CREATED": 1,
    "PICKED": 2,
    "LOADED": 2,
    "APPOINTMENT_CONFIRMED": 3,
    "GOODS_ISSUED": 2,
    "ASN_SENT": 2,
    "DELIVERED": 5,
}
_COLLECT_BASELINE = {
    "ORDER_RECEIVED": 0,
    "ORDER_CONFIRMED": 1,
    "MATERIAL_AVAILABLE": 1,
    "DELIVERY_CREATED": 1,
    "PICKED": 2,
    "READY_FOR_PICKUP": 2,
    "LOADED": 2,
    "GOODS_ISSUED": 2,
    "ASN_SENT": 2,
}


# --------------------------------------------------------------------------------------
# GC-01  On time and in full -> ON_TRACK, no risk rows, no penalty          (D1, D2)
# --------------------------------------------------------------------------------------


def test_gc01_on_time_and_in_full_is_on_track_with_no_risk() -> None:
    """GC-01: DELIVERED projects to Mar 7 inside Mar 5..Mar 9 and nothing is short -> nothing priced."""
    assessment = _assess(_plan(), [_otif_pct(), _fill_rate_floor()])

    result = assessment.result
    assert result.measured_milestone_code == "DELIVERED"
    assert result.projected_measured_date == _d(5)
    assert result.slack_days == 2  # Mar 9 - Mar 7
    assert result.timing_risks == ()
    assert result.slipping is False
    assert assessment.priced_risks == ()


# --------------------------------------------------------------------------------------
# GC-02  Late by 3 days, flat OTIF fee                                      (D2, D3, D5, D10)
# --------------------------------------------------------------------------------------


def test_gc02_late_three_days_charges_one_flat_otif_fee() -> None:
    """GC-02: PICKED slips 6 days -> DELIVERED Mar 12, 3 days after Mar 9 -> 3% x 2,000 x 20 = 1,200.00."""
    plan = _plan(milestones=_states(shifts={"PICKED": 6}))

    assessment = _assess(plan, [_otif_pct()])

    result = assessment.result
    assert result.projected_measured_date == _d(10)
    assert result.slack_days == -3
    assert len(result.timing_risks) == 1
    late = _timing(assessment, "LATE")
    assert (late.status, late.days_off, late.driver_milestone_code) == ("PROJECTED_BREACH", 3, "PICKED")
    priced = _priced(assessment, "LATE")
    assert priced.status == "PROJECTED_BREACH"
    assert priced.penalty_amount == 1200.0  # flat: the same fee at 1 day or 30 days late
    assert priced.priced_rule_ids == ("R-OTIF",)


# --------------------------------------------------------------------------------------
# GC-03  Late inside the grace period -> SLIPPING and $0                    (D10, D2)
# --------------------------------------------------------------------------------------


def test_gc03a_projected_one_day_late_inside_one_day_grace_is_slipping_and_free() -> None:
    """GC-03a: 1 day late, grace 1 -> chargeable days 0 -> $0 and PROJECTED_BREACH downgrades to SLIPPING."""
    plan = _plan(milestones=_states(shifts={"PICKED": 4}))

    assessment = _assess(plan, [_otif_pct(0.02, rule_id="R-CST-OTIF", grace=1)])

    assert assessment.result.projected_measured_date == _d(8)
    late = _timing(assessment, "LATE")
    assert (late.status, late.days_off) == ("PROJECTED_BREACH", 1)
    priced = _priced(assessment, "LATE")
    assert priced.status == "SLIPPING"
    assert priced.penalty_amount == 0.0
    assert priced.priced_rule_ids == ()
    assert priced.rule_breakdown[0].chargeable_days == 0


def test_gc03b_delivered_one_day_late_inside_grace_stays_breached_at_zero() -> None:
    """GC-03b: the same 1 day late but already delivered (actual date) stays BREACHED at $0."""
    plan = _plan(milestones=_states(actuals={"DELIVERED": 8}))

    assessment = _assess(plan, [_otif_pct(0.02, rule_id="R-CST-OTIF", grace=1)])

    late = _timing(assessment, "LATE")
    assert (late.status, late.days_off) == ("BREACHED", 1)
    priced = _priced(assessment, "LATE")
    assert priced.status == "BREACHED"
    assert priced.penalty_amount == 0.0


def test_gc03c_one_day_past_grace_charges_the_full_flat_fee() -> None:
    """GC-03c: 2 days late, grace 1 -> chargeable 1 -> flat 2% x 2,000 x 20 = 800.00 (not scaled by days)."""
    plan = _plan(milestones=_states(shifts={"PICKED": 5}))

    assessment = _assess(plan, [_otif_pct(0.02, rule_id="R-CST-OTIF", grace=1)])

    assert _timing(assessment, "LATE").days_off == 2
    priced = _priced(assessment, "LATE")
    assert priced.status == "PROJECTED_BREACH"
    assert priced.penalty_amount == 800.0
    assert priced.rule_breakdown[0].chargeable_days == 1


# --------------------------------------------------------------------------------------
# GC-04  Late beyond grace, per-day accrual, cap applied after accrual      (D5, D10)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actual_offset", "days_late", "chargeable", "expected"),
    [
        pytest.param(10, 3, 2, 800.0, id="cap-not-binding"),
        pytest.param(12, 5, 4, 1000.0, id="cap-binds"),
    ],
)
def test_gc04_per_day_accrual_with_grace_then_cap(
    actual_offset: int, days_late: int, chargeable: int, expected: float
) -> None:
    """GC-04: 1% of PO per chargeable day (400.00/day), grace 1, cap 1,000.00 applied to the accrued total."""
    rule = _rule(
        "R-DAILY",
        "OTIF_LATE",
        CalcType.PERCENT_OF_PO,
        0.01,
        cap_amount=1000.0,
        applies_per=APPLIES_PER_DAY,
        grace_period_days=1,
    )
    plan = _plan(milestones=_states(actuals={"DELIVERED": actual_offset}))

    assessment = _assess(plan, [rule])

    late = _timing(assessment, "LATE")
    assert (late.status, late.days_off) == ("BREACHED", days_late)
    priced = _priced(assessment, "LATE")
    assert priced.rule_breakdown[0].chargeable_days == chargeable
    assert priced.penalty_amount == expected
    assert priced.status == "BREACHED"


# --------------------------------------------------------------------------------------
# GC-05  TIERED delay: CLIFF vs MARGINAL                                    (D5, D10)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days_late", "expected"),
    [
        pytest.param(2, 400.0, id="2d-first-band"),
        pytest.param(3, 800.0, id="3d-band-edge-belongs-to-upper-band"),
        pytest.param(5, 800.0, id="5d-middle-band"),
        pytest.param(7, 1600.0, id="7d-band-edge-top-band"),
    ],
)
def test_gc05a_tiered_cliff_prices_the_single_band_containing_days_late(
    days_late: int, expected: float
) -> None:
    """GC-05a: bands [0,3) 1%, [3,7) 2%, [7,inf) 4% of PO value 40,000.00; CLIFF uses one band's rate."""
    plan = _plan(milestones=_states(actuals={"DELIVERED": 7 + days_late}))

    assessment = _assess(plan, [_tiered_days_rule("CLIFF")])

    assert _timing(assessment, "LATE").days_off == days_late
    assert _priced(assessment, "LATE").penalty_amount == expected


@pytest.mark.parametrize(
    ("days_late", "expected"),
    [
        pytest.param(2, 800.0, id="2d"),
        pytest.param(3, 1200.0, id="3d"),
        pytest.param(5, 2800.0, id="5d"),
        pytest.param(7, 4400.0, id="7d"),
        pytest.param(9, 7600.0, id="9d"),
    ],
)
def test_gc05b_tiered_marginal_sums_each_band_slice_in_days(days_late: int, expected: float) -> None:
    """GC-05b: MARGINAL applies each band's rate to the DAYS inside it (rate x days-in-band x 40,000.00)."""
    plan = _plan(milestones=_states(actuals={"DELIVERED": 7 + days_late}))

    assessment = _assess(plan, [_tiered_days_rule("MARGINAL")])

    assert _timing(assessment, "LATE").days_off == days_late
    assert _priced(assessment, "LATE").penalty_amount == expected


# --------------------------------------------------------------------------------------
# GC-06  Early delivery: only a window-violation rule charges it            (D12, D2)
# --------------------------------------------------------------------------------------


def test_gc06a_early_delivery_is_charged_by_the_window_violation_rule() -> None:
    """GC-06a: window Mar 12..Mar 16, DELIVERED Mar 7 -> 5 days early -> 3% x 2,000 x 20 = 1,200.00."""
    plan = _plan(window_start=_d(10), window_end=_d(14))
    dwv = _rule("R-DWV", "DELIVERY_WINDOW_VIOLATION", CalcType.PERCENT_OF_PO, 0.03)

    assessment = _assess(plan, [_otif_pct(), dwv])

    assert [t.risk_type for t in assessment.result.timing_risks] == ["EARLY"]
    early = _timing(assessment, "EARLY")
    assert (early.status, early.days_off) == ("PROJECTED_BREACH", -5)
    priced = _priced(assessment, "EARLY")
    assert priced.penalty_amount == 1200.0
    assert priced.priced_rule_ids == ("R-DWV",)
    assert priced.rule_breakdown[0].chargeable_days == 5


def test_gc06b_early_delivery_without_a_window_violation_rule_is_free() -> None:
    """GC-06b: the same 5 days early with only an OTIF_LATE rule -> no rule applies -> $0, SLIPPING."""
    plan = _plan(window_start=_d(10), window_end=_d(14))

    assessment = _assess(plan, [_otif_pct()])

    assert _timing(assessment, "EARLY").days_off == -5
    priced = _priced(assessment, "EARLY")
    assert priced.penalty_amount == 0.0
    assert priced.status == "SLIPPING"
    assert priced.priced_rule_ids == ()
    assert priced.rule_breakdown == ()


# --------------------------------------------------------------------------------------
# GC-07  Past the cancel date -> NOT_DELIVERED, full quantity, LATE suppressed   (D13, D11, D2)
# --------------------------------------------------------------------------------------


def test_gc07a_projected_past_cancel_date_prices_full_quantity_through_shortage_rules() -> None:
    """GC-07a: DELIVERED Mar 12 > cancel Mar 10 -> NOT_DELIVERED (LATE suppressed); 2,000 units short."""
    plan = _plan(milestones=_states(shifts={"PICKED": 6}), cancel_date=_d(8))

    assessment = _assess(plan, [_otif_pct(), _short_per_unit(1.5, threshold=0.05)])

    assert assessment.result.projected_measured_date == _d(10)
    assert [t.risk_type for t in assessment.result.timing_risks] == ["NOT_DELIVERED"]
    risk = _timing(assessment, "NOT_DELIVERED")
    assert (risk.status, risk.days_off) == ("PROJECTED_BREACH", 2)
    assert [p.risk_type for p in assessment.priced_risks] == ["NOT_DELIVERED"]
    priced = _priced(assessment, "NOT_DELIVERED")
    assert priced.penalty_amount == 2850.0  # (2,000 - 5% x 2,000) x 1.50
    assert priced.priced_rule_ids == ("R-SHORT",)
    assert priced.rule_breakdown[0].shortfall_quantity == 2000.0


def test_gc07b_run_date_after_cancel_date_is_breached_and_overdue_steps_complete_today() -> None:
    """GC-07b: as_of Mar 11 > cancel Mar 10 -> BREACHED; PICKED (planned Mar 10) is assumed done today."""
    plan = _plan(
        milestones=_states(
            shifts={"PICKED": 6},
            actuals={
                "ORDER_RECEIVED": 0,
                "ORDER_CONFIRMED": 1,
                "MATERIAL_AVAILABLE": 1,
                "TENDER_ACCEPTED": 2,
                "DELIVERY_CREATED": 1,
                "APPOINTMENT_CONFIRMED": 3,
            },
        ),
        cancel_date=_d(8),
        as_of=_d(9),
    )

    assessment = _assess(plan, [_otif_pct(), _short_per_unit(1.5, threshold=0.05)])

    picked = next(m for m in assessment.result.milestones if m.code == "PICKED")
    assert picked.projected_date == _d(9)  # overdue step floors at the run date (D11)
    assert assessment.result.projected_measured_date == _d(11)  # PICKED + GI + 2-day transit
    assert [t.risk_type for t in assessment.result.timing_risks] == ["NOT_DELIVERED"]
    risk = _timing(assessment, "NOT_DELIVERED")
    assert (risk.status, risk.days_off) == ("BREACHED", 3)
    assert _priced(assessment, "NOT_DELIVERED").penalty_amount == 2850.0
    assert _priced(assessment, "NOT_DELIVERED").status == "BREACHED"


# --------------------------------------------------------------------------------------
# GC-08  SHORT, SHORTFALL_VALUE basis, threshold is a fill-rate floor      (D1, D5)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shortfall", "expected"),
    [
        pytest.param(100.0, 60.0, id="fill-95pct"),
        pytest.param(200.0, 120.0, id="fill-90pct-scales-with-shortfall"),
    ],
)
def test_gc08_short_below_fill_rate_floor_charges_rate_times_shortfall_value(
    shortfall: float, expected: float
) -> None:
    """GC-08: floor 98%; 3% x (shortfall x 20.00). 100 short -> 3% x 2,000 = 60.00; 200 short -> 120.00."""
    assessment = _assess(
        _plan(), [_fill_rate_floor()], shortfall=shortfall, shortfall_status="PROJECTED_BREACH"
    )

    assert assessment.result.timing_risks == ()
    priced = _priced(assessment, "SHORT")
    assert priced.status == "PROJECTED_BREACH"
    assert priced.penalty_amount == expected
    assert priced.priced_rule_ids == ("R-FILL",)
    assert priced.rule_breakdown[0].shortfall_quantity == shortfall


# --------------------------------------------------------------------------------------
# GC-09  SHORT, grace-band semantics (PER_UNIT and PERCENT_OF_PO)          (D1, D5)
# --------------------------------------------------------------------------------------


def test_gc09a_per_unit_charges_only_units_beyond_the_grace_band() -> None:
    """GC-09a: band 2% x 2,000 = 40 units; 100 short -> (100 - 40) x 2.50 = 150.00."""
    assessment = _assess(
        _plan(), [_short_per_unit(2.5, threshold=0.02)], shortfall=100.0, shortfall_status="PROJECTED_BREACH"
    )

    assert _priced(assessment, "SHORT").penalty_amount == 150.0


@pytest.mark.parametrize("shortfall", [120.0, 1000.0])
def test_gc09b_percent_of_po_is_flat_once_past_the_band(shortfall: float) -> None:
    """GC-09b: band 5% x 2,000 = 100 units; past it, 2% x 2,000 x 20 = 800.00 whatever the shortfall size."""
    rule = _rule(
        "R-SHORT-PCT", "SHORT_SHIP", CalcType.PERCENT_OF_PO, 0.02, threshold_pct=0.05, basis_type="PO_VALUE"
    )

    assessment = _assess(_plan(), [rule], shortfall=shortfall, shortfall_status="PROJECTED_BREACH")

    assert _priced(assessment, "SHORT").penalty_amount == 800.0


# --------------------------------------------------------------------------------------
# GC-10  SHORT exactly at the threshold is compliant                        (D1)
# --------------------------------------------------------------------------------------


def test_gc10a_shortfall_equal_to_the_grace_band_is_free_and_one_unit_more_is_charged() -> None:
    """GC-10a: band 5% x 2,000 = 100 units. 100 short -> $0 (SLIPPING); 101 short -> 1 x 2.50 = 2.50."""
    rule = _short_per_unit(2.5, threshold=0.05)

    at_band = _assess(_plan(), [rule], shortfall=100.0, shortfall_status="PROJECTED_BREACH")
    over_band = _assess(_plan(), [rule], shortfall=101.0, shortfall_status="PROJECTED_BREACH")

    assert _priced(at_band, "SHORT").penalty_amount == 0.0
    assert _priced(at_band, "SHORT").status == "SLIPPING"
    assert _priced(over_band, "SHORT").penalty_amount == 2.5
    assert _priced(over_band, "SHORT").status == "PROJECTED_BREACH"


def test_gc10b_fill_rate_exactly_at_the_floor_is_compliant_and_one_unit_below_is_charged() -> None:
    """GC-10b: floor 98%. 40 short -> fill 98.00% -> $0; 41 short -> 97.95% -> 3% x (41 x 20) = 24.60."""
    at_floor = _assess(_plan(), [_fill_rate_floor()], shortfall=40.0, shortfall_status="PROJECTED_BREACH")
    below_floor = _assess(_plan(), [_fill_rate_floor()], shortfall=41.0, shortfall_status="PROJECTED_BREACH")

    assert _priced(at_floor, "SHORT").penalty_amount == 0.0
    assert _priced(at_floor, "SHORT").status == "SLIPPING"
    assert _priced(below_floor, "SHORT").penalty_amount == 24.6
    assert _priced(below_floor, "SHORT").status == "PROJECTED_BREACH"


# --------------------------------------------------------------------------------------
# GC-11  ASN sent 5 days after goods issue                                  (D4, D10)
# --------------------------------------------------------------------------------------


def _asn_plan(asn_planned_offset: int = 5) -> PlanTimelineInput:
    return _plan(
        milestones=(
            _ms("GOODS_ISSUED", actual=_d(0)),
            _ms("ASN_SENT", planned=_d(asn_planned_offset)),
        )
    )


def test_gc11a_asn_five_days_after_goods_issue_charges_the_flat_asn_fee() -> None:
    """GC-11a: GOODS_ISSUED Mar 2, ASN planned Mar 7 -> 5 days late, grace 0 -> flat 250.00."""
    rule = _rule("R-ASN", "ASN_LATE", CalcType.FLAT_FEE, 250.0)

    assessment = _assess(_asn_plan(), [rule])

    assert [t.risk_type for t in assessment.result.timing_risks] == ["ASN_LATE"]  # delivery itself is on time
    asn = _timing(assessment, "ASN_LATE")
    assert (asn.status, asn.days_off, asn.driver_milestone_code) == ("PROJECTED_BREACH", 5, "ASN_SENT")
    priced = _priced(assessment, "ASN_LATE")
    assert priced.penalty_amount == 250.0
    assert priced.priced_rule_ids == ("R-ASN",)


def test_gc11b_per_day_asn_fee_charges_only_days_beyond_grace() -> None:
    """GC-11b: 50.00/day, grace 2 -> chargeable 5 - 2 = 3 days -> 150.00."""
    rule = _rule(
        "R-ASN-DAY", "ASN_LATE", CalcType.FLAT_FEE, 50.0, applies_per=APPLIES_PER_DAY, grace_period_days=2
    )

    assessment = _assess(_asn_plan(), [rule])

    priced = _priced(assessment, "ASN_LATE")
    assert priced.rule_breakdown[0].chargeable_days == 3
    assert priced.penalty_amount == 150.0


def test_gc11c_asn_late_inside_grace_is_slipping_and_asn_on_goods_issue_day_is_no_risk() -> None:
    """GC-11c: grace 5 absorbs 5 days -> $0 SLIPPING. ASN sent the same day as goods issue -> no risk."""
    rule = _rule("R-ASN", "ASN_LATE", CalcType.FLAT_FEE, 250.0, grace_period_days=5)

    absorbed = _assess(_asn_plan(), [rule])
    on_time = _assess(_asn_plan(asn_planned_offset=0), [rule])

    assert _priced(absorbed, "ASN_LATE").penalty_amount == 0.0
    assert _priced(absorbed, "ASN_LATE").status == "SLIPPING"
    assert on_time.result.timing_risks == ()
    assert on_time.priced_risks == ()


# --------------------------------------------------------------------------------------
# GC-12  Stacking inside one risk: two rules on the same LATE, SUM vs MAX  (D5)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("stacking_mode", "expected"), [("SUM", 1700.0), ("MAX", 1200.0)])
def test_gc12_two_otif_rules_on_one_late_risk_stack_by_retailer_mode(
    stacking_mode: str, expected: float
) -> None:
    """GC-12: rule A 3% of PO = 1,200.00, rule B flat 500.00; SUM -> 1,700.00, MAX -> 1,200.00."""
    plan = _plan(milestones=_states(shifts={"PICKED": 6}))
    rule_a = _otif_pct(rule_id="R-A")
    rule_b = _rule("R-B", "OTIF_LATE", CalcType.FLAT_FEE, 500.0)

    assessment = _assess(plan, [rule_a, rule_b], stacking=stacking_mode)

    priced = _priced(assessment, "LATE")
    assert priced.penalty_amount == expected
    assert priced.priced_rule_ids == ("R-A", "R-B")
    assert [charge.amount for charge in priced.rule_breakdown] == [1200.0, 500.0]


# --------------------------------------------------------------------------------------
# GC-13  Two plans compete for one pool: earlier need date wins            (D9)
# --------------------------------------------------------------------------------------


def test_gc13_earlier_need_date_is_served_first_and_the_later_plan_is_short() -> None:
    """GC-13: on hand 100; A needs 80 by Mar 9, B needs 80 by Mar 12 -> A covered, B gets 20, 60 short."""
    demand_a = _demand("PLAN-A", "LINE-1", 80.0, need_offset=7, plan_number="A")
    demand_b = _demand("PLAN-B", "LINE-1", 80.0, need_offset=10, plan_number="B")

    coverages = allocate_supply(on_hand=100.0, receipts=[], demands=[demand_b, demand_a], as_of=D0)
    outcomes = {o.plan_id: o for o in plan_outcomes(coverages)}

    assert [c.plan_id for c in coverages] == ["PLAN-A", "PLAN-B"]
    assert outcomes["PLAN-A"].shortfall_quantity == 0.0
    assert outcomes["PLAN-A"].full_cover_date == _d(7)
    assert outcomes["PLAN-B"].on_time_quantity == 20.0
    assert outcomes["PLAN-B"].shortfall_quantity == 60.0
    assert outcomes["PLAN-B"].material_available_not_before is None
    assert outcomes["PLAN-B"].cause_code == "DEMAND_EXCEEDS_SUPPLY"
    assert outcomes["PLAN-B"].cause_source_id is None

    basis_b = PricingBasis(quantity=80.0, unit_cost=19.50, unit_price=20.00)
    assessment = _assess(
        _plan(),
        [_fill_rate_floor()],
        basis=basis_b,
        shortfall=outcomes["PLAN-B"].shortfall_quantity,
        shortfall_status="PROJECTED_BREACH",
    )
    assert [p.risk_type for p in assessment.priced_risks] == ["SHORT"]
    assert _priced(assessment, "SHORT").penalty_amount == 36.0  # 3% x (60 x 20.00)


# --------------------------------------------------------------------------------------
# GC-14  Cause codes come from baseline vs available dates                 (D9, D8)
# --------------------------------------------------------------------------------------


_PROD = "PRODUCTION_ORDER"


@pytest.mark.parametrize(
    ("source_type", "source_id", "baseline_offset", "reason", "expected_code", "expected_source"),
    [
        pytest.param(_PROD, "PRD-1", 6, "PRODUCTION_DELAY", "PRODUCTION_DELAY", "PRD-1", id="production"),
        pytest.param("QA_LOT", "QA-1", 6, "QA_HOLD", "QA_HOLD", "QA-1", id="qa-hold"),
        pytest.param(
            _PROD, "PRD-2", 9, "PRODUCTION_DELAY", "DEMAND_EXCEEDS_SUPPLY", None, id="never-on-time"
        ),
        pytest.param(
            _PROD, "PRD-3", None, "PRODUCTION_DELAY", "DEMAND_EXCEEDS_SUPPLY", None, id="no-baseline"
        ),
        pytest.param(_PROD, "PRD-4", 6, None, "SUPPLY_DELAYED", "PRD-4", id="no-reason-code"),
    ],
)
def test_gc14_cause_code_depends_on_whether_the_receipt_was_ever_on_time(
    source_type: str,
    source_id: str,
    baseline_offset: int | None,
    reason: str | None,
    expected_code: str,
    expected_source: str | None,
) -> None:
    """GC-14: need Mar 9, 100 units arrive Mar 14. Baseline Mar 8 -> delayed supply with its reason;
    baseline after Mar 9 (or none) -> the network never had it in time -> DEMAND_EXCEEDS_SUPPLY."""
    receipt = _receipt(
        source_id,
        100.0,
        available_offset=12,
        baseline_offset=baseline_offset,
        reason=reason,
        source_type=source_type,
    )

    outcome = _single_outcome(on_hand=0.0, receipts=[receipt], line_quantities=(100.0,))

    assert outcome.shortfall_quantity == 0.0
    assert outcome.material_available_not_before == _d(12)
    assert outcome.cause_code == expected_code
    assert outcome.cause_source_id == expected_source


# --------------------------------------------------------------------------------------
# GC-15  Zero stock, production delayed -> LATE only, no SHORT              (D3, D8, D9)
# --------------------------------------------------------------------------------------


def test_gc15_zero_stock_with_a_late_receipt_that_covers_everything_is_late_not_short() -> None:
    """GC-15: 100 units arrive Mar 14 (need Mar 9) -> MATERIAL_AVAILABLE Mar 14 -> DELIVERED Mar 17 -> LATE 8."""
    receipt = _receipt("PRD-1", 100.0, available_offset=12, baseline_offset=6, reason="PRODUCTION_DELAY")
    outcome = _single_outcome(on_hand=0.0, receipts=[receipt], line_quantities=(100.0,))
    assert outcome.shortfall_quantity == 0.0
    assert outcome.material_available_not_before == _d(12)
    plan = _plan(not_before={"MATERIAL_AVAILABLE": outcome.material_available_not_before})

    assessment = _assess(plan, [_otif_pct(), _fill_rate_floor()], basis=BASIS_100)

    assert assessment.result.projected_measured_date == _d(15)
    assert [p.risk_type for p in assessment.priced_risks] == ["LATE"]
    late = _timing(assessment, "LATE")
    assert (late.status, late.days_off, late.driver_milestone_code) == (
        "PROJECTED_BREACH",
        8,
        "MATERIAL_AVAILABLE",
    )
    assert _priced(assessment, "LATE").penalty_amount == 60.0  # 3% x 100 x 20.00
    assert outcome.cause_code == "PRODUCTION_DELAY"


# --------------------------------------------------------------------------------------
# GC-16  Partial stock on time -> remainder is SHORT, not waited for       (D8, D9)
# --------------------------------------------------------------------------------------


def test_gc16_partial_on_time_stock_ships_now_and_the_remainder_is_short() -> None:
    """GC-16: 60 on hand + 40 arriving Mar 14, need 100 by Mar 9 -> ship 60, 40 SHORT, no LATE."""
    receipt = _receipt("PRD-1", 40.0, available_offset=12, baseline_offset=6, reason="PRODUCTION_DELAY")

    outcome = _single_outcome(on_hand=60.0, receipts=[receipt], line_quantities=(100.0,))

    assert outcome.on_time_quantity == 60.0
    assert outcome.shortfall_quantity == 40.0
    assert outcome.material_available_not_before is None  # the late 40 are not waited for
    assert outcome.full_cover_date == _d(12)
    assert outcome.cause_code == "PRODUCTION_DELAY"

    assessment = _assess(
        _plan(),
        [_otif_pct(), _fill_rate_floor()],
        basis=BASIS_100,
        shortfall=outcome.shortfall_quantity,
        shortfall_status="PROJECTED_BREACH",
    )
    assert assessment.result.timing_risks == ()
    assert [p.risk_type for p in assessment.priced_risks] == ["SHORT"]
    assert _priced(assessment, "SHORT").penalty_amount == 24.0  # 3% x (40 x 20.00)


# --------------------------------------------------------------------------------------
# GC-17  Nothing on time, part arrives late, part never -> late ship + SHORT  (D8)
# --------------------------------------------------------------------------------------


def test_gc17_nothing_on_time_ships_late_with_only_the_never_covered_quantity_short() -> None:
    """GC-17 (B4 target): need 100 by Mar 9, nothing on hand, 70 arrive Mar 14 -> ship Mar 14, 30 SHORT."""
    receipt = _receipt("PRD-1", 70.0, available_offset=12, baseline_offset=6, reason="PRODUCTION_DELAY")
    demand = _demand("PLAN-GC", "LINE-1", 100.0, need_offset=7)

    coverage = allocate_supply(on_hand=0.0, receipts=[receipt], demands=[demand], as_of=D0)[0]
    outcome = plan_outcomes([coverage])[0]

    assert coverage.on_time_quantity == 0.0
    assert coverage.uncovered_quantity == 30.0
    assert outcome.shortfall_quantity == 30.0
    assert outcome.cause_code == "PRODUCTION_DELAY"
    assert outcome.material_available_not_before == _d(12)  # the plan ships late, it is not silently on time


# --------------------------------------------------------------------------------------
# GC-18  LATE and SHORT coexist; SUM adds, MAX takes the larger             (D3, D8)
# --------------------------------------------------------------------------------------


def test_gc18_late_and_short_coexist_and_plan_level_stacking_sums_or_takes_the_max() -> None:
    """GC-18: lines 50 + 50, one receipt of 50 on Mar 14 -> LATE 8 days (60.00) and 50 SHORT (30.00)."""
    receipt = _receipt("PRD-1", 50.0, available_offset=12, baseline_offset=6, reason="PRODUCTION_DELAY")
    outcome = _single_outcome(on_hand=0.0, receipts=[receipt], line_quantities=(50.0, 50.0))
    assert outcome.shortfall_quantity == 50.0
    assert outcome.material_available_not_before == _d(12)
    assert outcome.cause_code == "PRODUCTION_DELAY"
    plan = _plan(not_before={"MATERIAL_AVAILABLE": outcome.material_available_not_before})

    assessment = _assess(
        plan,
        [_otif_pct(), _fill_rate_floor()],
        basis=BASIS_100,
        shortfall=outcome.shortfall_quantity,
        shortfall_status="PROJECTED_BREACH",
    )

    assert [p.risk_type for p in assessment.priced_risks] == ["LATE", "SHORT"]
    assert _priced(assessment, "LATE").penalty_amount == 60.0
    assert _priced(assessment, "SHORT").penalty_amount == 30.0  # 3% x (50 x 20.00), fill rate 50%
    amounts = [p.penalty_amount for p in assessment.priced_risks]
    assert stack_amounts(amounts, "SUM") == 90.0
    assert stack_amounts(amounts, "MAX") == 60.0


# --------------------------------------------------------------------------------------
# GC-19  COLLECT plan is measured when it is ready for pickup               (D2)
# --------------------------------------------------------------------------------------


def test_gc19_collect_plan_is_measured_at_ready_for_pickup_and_ignores_transit() -> None:
    """GC-19: window Mar 3..Mar 6, PICKED +3 -> RFP Mar 7 -> 1 day late. PREPAID would be 3 days late."""
    collect_plan = _plan(
        freight_term="COLLECT",
        milestones=_states("COLLECT", shifts={"PICKED": 3}),
        window_start=_d(1),
        window_end=_d(4),
    )
    prepaid_plan = _plan(milestones=_states(shifts={"PICKED": 3}), window_start=_d(1), window_end=_d(4))

    collect = _assess(collect_plan, [_otif_pct(0.02)])
    prepaid = _assess(prepaid_plan, [_otif_pct(0.02)])

    assert collect.result.measured_milestone_code == "READY_FOR_PICKUP"
    assert collect.result.projected_measured_date == _d(5)
    assert "DELIVERED" not in {m.code for m in collect.result.milestones}
    late = _timing(collect, "LATE")
    assert (late.status, late.days_off, late.driver_milestone_code) == ("PROJECTED_BREACH", 1, "PICKED")
    assert _priced(collect, "LATE").penalty_amount == 800.0  # 2% x 2,000 x 20.00

    assert prepaid.result.measured_milestone_code == "DELIVERED"
    assert prepaid.result.projected_measured_date == _d(7)
    assert _timing(prepaid, "LATE").days_off == 3


# --------------------------------------------------------------------------------------
# GC-20  Mitigation ladder: net saving = before - after - fixed cost       (D14, D11)
# --------------------------------------------------------------------------------------


def test_gc20_mitigation_options_cure_lateness_and_rank_by_net_saving() -> None:
    """GC-20: window ends Mar 8, PICKED +3 -> DELIVERED Mar 9 -> 1 day late, 1,200.00. Rank by net saving."""
    plan = _plan(milestones=_states(shifts={"PICKED": 3}), window_end=_d(6))
    rules = [_otif_pct()]
    assessment = _assess(plan, rules)
    late = _timing(assessment, "LATE")
    assert (late.days_off, late.driver_milestone_code) == (1, "PICKED")
    penalty_before = _priced(assessment, "LATE").penalty_amount
    assert penalty_before == 1200.0
    situation = PlanSituation(
        plan=plan,
        definitions=tuple(_definitions()),
        baseline_result=assessment.result,
        risk_types=frozenset({"LATE"}),
        driver_code="PICKED",
        driver_reason="WAVE_NOT_RELEASED",
        penalty_before=penalty_before,
        shortfall_quantity=0.0,
    )

    options = evaluate_mitigations(situation, _make_reprice(rules))

    by_code = {o.action_code: o for o in options}
    pick = by_code["PRIORITIZE_PICK"]
    assert (pick.feasible, pick.penalty_before, pick.penalty_after) == (True, 1200.0, 0.0)
    assert (pick.action_cost, pick.net_saving) == (300.0, 900.0)
    freight = by_code["EXPEDITE_FREIGHT"]
    assert (freight.feasible, freight.penalty_after) == (True, 0.0)
    assert (freight.action_cost, freight.net_saving) == (900.0, 300.0)
    accept = by_code["ACCEPT"]
    assert (accept.penalty_after, accept.action_cost, accept.net_saving) == (1200.0, 0.0, 0.0)
    date_change = by_code["REQUEST_DATE_CHANGE"]
    assert (date_change.penalty_after, date_change.net_saving) == (600.0, 600.0)  # exactly 50% of before
    team = by_code["TEAM_DRIVERS"]
    assert (team.penalty_after, team.net_saving) == (0.0, -1000.0)  # cures it, costs more than it saves
    assert by_code["REBOOK_CARRIER"].feasible is False
    assert by_code["REBOOK_CARRIER"].infeasible_reason == ROOT_CAUSE_MISMATCH

    feasible_order = [o.action_code for o in options if o.feasible]
    assert feasible_order == [
        "PRIORITIZE_PICK",
        "REQUEST_DATE_CHANGE",
        "EXPEDITE_FREIGHT",
        "ACCEPT",
        "TEAM_DRIVERS",
    ]
    assert [o.rank_no for o in options if o.feasible] == [1, 2, 3, 4, 5]


# --------------------------------------------------------------------------------------
# GC-21  Alert lifecycle
# --------------------------------------------------------------------------------------


def test_gc21a_alert_walks_new_worse_better_then_auto_clears() -> None:
    """GC-21a: Mar 10 NEW (600) -> Mar 11 WORSE (1,800) -> Mar 12 BETTER (1,200) -> Mar 13 RESOLVED/AUTO_CLEARED."""
    day1, day2, day3, day4 = date(2026, 3, 10), date(2026, 3, 11), date(2026, 3, 12), date(2026, 3, 13)

    created = plan_alert_changes(day1, [_current("PROJECTED_BREACH", 600.0, 1)], [], now=NOW)[0]
    assert created.kind == "CREATE"
    assert created.fields["status"] == "NEW"
    assert created.fields["change_type"] == "NEW"
    assert created.fields["last_penalty_amount"] == 600.0

    tracked = _alert_state("NEW", day1, prev=(None, None), last=(600.0, 1))
    worse = plan_alert_changes(day2, [_current("PROJECTED_BREACH", 1800.0, 3)], [tracked], now=NOW)[0]
    assert worse.kind == "UPDATE"
    assert worse.fields["change_type"] == "WORSE"
    assert (worse.fields["prev_penalty_amount"], worse.fields["last_penalty_amount"]) == (600.0, 1800.0)
    assert (worse.fields["prev_days_off"], worse.fields["last_days_off"]) == (1, 3)

    tracked = _alert_state("NEW", day2, prev=(600.0, 1), last=(1800.0, 3))
    better = plan_alert_changes(day3, [_current("PROJECTED_BREACH", 1200.0, 2)], [tracked], now=NOW)[0]
    assert better.kind == "UPDATE"
    assert better.fields["change_type"] == "BETTER"
    assert (better.fields["prev_penalty_amount"], better.fields["last_penalty_amount"]) == (1800.0, 1200.0)

    tracked = _alert_state("NEW", day3, prev=(1800.0, 3), last=(1200.0, 2))
    cleared = plan_alert_changes(day4, [], [tracked], now=NOW)[0]
    assert cleared.kind == "CLOSE"
    assert cleared.fields["status"] == "RESOLVED"
    assert cleared.fields["closed_reason"] == "AUTO_CLEARED"
    assert cleared.fields["is_tracking"] is False


def test_gc21b_breached_with_a_nonzero_penalty_closes_as_penalty_incurred() -> None:
    """GC-21b: an open alert whose risk is now BREACHED at 1,200.00 closes PENALTY_INCURRED (BREACHED)."""
    tracked = _alert_state("ACKNOWLEDGED", date(2026, 3, 12), prev=(None, None), last=(1200.0, 3))

    change = plan_alert_changes(date(2026, 3, 13), [_current("BREACHED", 1200.0, 3)], [tracked], now=NOW)[0]

    assert change.kind == "CLOSE"
    assert change.fields["status"] == "PENALTY_INCURRED"
    assert change.fields["closed_reason"] == "BREACHED"


def test_gc21c_breached_at_zero_penalty_does_not_close_and_action_taken_clears_as_fix_worked() -> None:
    """GC-21c: BREACHED but $0 (inside grace) only updates; ACTION_TAKEN + risk gone -> FIX_WORKED."""
    tracked = _alert_state("NEW", date(2026, 3, 12), prev=(None, None), last=(0.0, 1))
    acted = _alert_state("ACTION_TAKEN", date(2026, 3, 12), prev=(None, None), last=(1200.0, 3))

    zero = plan_alert_changes(date(2026, 3, 13), [_current("BREACHED", 0.0, 1)], [tracked], now=NOW)[0]
    fixed = plan_alert_changes(date(2026, 3, 13), [], [acted], now=NOW)[0]

    assert zero.kind == "UPDATE"
    assert "status" not in zero.fields
    assert fixed.kind == "CLOSE"
    assert fixed.fields["closed_reason"] == "FIX_WORKED"


# --------------------------------------------------------------------------------------
# GC-22  Dispute verdicts: claim vs recomputed amount                        (D1, D5, D10)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actual_offset", "grace", "claimed", "verdict", "computed", "delta"),
    [
        pytest.param(10, 0, 1200.0, DisputeVerdict.PAY_FULL, 1200.0, 0.0, id="claim-equals-computed"),
        pytest.param(10, 0, 1500.0, DisputeVerdict.PAY_PARTIAL, 1200.0, 300.0, id="claim-exceeds-computed"),
        pytest.param(10, 0, 1000.0, DisputeVerdict.PAY_FULL, 1200.0, -200.0, id="claim-below-computed"),
        pytest.param(7, 0, 1200.0, DisputeVerdict.NO_PAY, 0.0, 1200.0, id="delivered-on-deadline"),
        pytest.param(8, 1, 1200.0, DisputeVerdict.NO_PAY, 0.0, 1200.0, id="inside-grace"),
        pytest.param(9, 1, 1200.0, DisputeVerdict.PAY_FULL, 1200.0, 0.0, id="one-day-past-grace"),
    ],
)
def test_gc22_dispute_verdict_follows_the_recomputed_penalty(
    actual_offset: int,
    grace: int,
    claimed: float,
    verdict: DisputeVerdict,
    computed: float,
    delta: float,
) -> None:
    """GC-22: required Mar 9, 3% OTIF flat on 2,000 x 20.00 = 1,200.00. Computed 0 -> NO_PAY; claim above
    computed -> PAY_PARTIAL with the delta; claim at or below computed -> PAY_FULL (never topped up)."""
    facts = DisputeFacts(
        order_qty=2000,
        unit_price=20.00,
        delivered_qty=2000.0,
        required_delivery_date=_d(7),
        actual_delivery_date=_d(actual_offset),
        grace_period_days=grace,
    )

    result = recompute_dispute(_otif_pct(), facts, claimed)

    assert result.computed_amount == computed
    assert result.verdict == verdict
    assert result.delta_amount == delta


# --------------------------------------------------------------------------------------
# GC-23  Penalty value basis is the PO unit price, never Mars standard cost  (D5)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("unit_cost", [19.50, 10.0, None], ids=["cost-19.50", "cost-10.00", "cost-unknown"])
@pytest.mark.parametrize("basis_type", [None, "PO_VALUE", "COST_OF_GOODS"])
def test_gc23a_late_fee_prices_at_unit_price_whatever_the_rule_basis_type_or_cost(
    basis_type: str | None, unit_cost: float | None
) -> None:
    """GC-23a: 3% of 2,000 units = 1,200.00 at price 20.00 (not 1,170.00 at cost 19.50)."""
    rule = _rule("R-OTIF", "OTIF_LATE", CalcType.PERCENT_OF_PO, 0.03, basis_type=basis_type)
    basis = PricingBasis(quantity=2000.0, unit_cost=unit_cost, unit_price=20.00)

    priced = price_risk("LATE", "PROJECTED_BREACH", 1, None, basis, [rule], "SUM")

    assert priced.penalty_amount == 1200.0
    assert priced.rule_breakdown[0].unit_amount == 20.0


def test_gc23b_shortfall_value_prices_at_unit_price_not_cost() -> None:
    """GC-23b: 100 units short, 3% of shortfall value = 3% x (100 x 20.00) = 60.00 (not 58.50 at cost 19.50)."""
    priced = price_risk("SHORT", "PROJECTED_BREACH", None, 100.0, BASIS, [_fill_rate_floor()], "SUM")

    assert priced.penalty_amount == 60.0
    assert priced.rule_breakdown[0].unit_amount == 20.0


# --------------------------------------------------------------------------------------
# GC-24  ASN dispute: measured goods issue -> ASN sent, not delivery date    (D4)
# --------------------------------------------------------------------------------------


class TestGc24AsnDispute:
    """GC-24 (B3): ASN_LATE days = asn_sent_date - goods_issued_date - grace. Delivery date is irrelevant."""

    def test_gc24a_flat_fee_claim_equal_to_computed_is_pay_full(self) -> None:
        """Goods issued Mar 2, ASN Mar 7 (5 days, grace 0), delivered on time Mar 8 -> 250.00 flat."""
        rule = _rule("R-ASN", "ASN_LATE", CalcType.FLAT_FEE, 250.0)
        facts = _asn_facts(asn_offset=5, goods_issued_offset=0, delivered_offset=6)

        result = recompute_dispute(rule, facts, 250.0)

        assert result.computed_amount == 250.0
        assert result.verdict == DisputeVerdict.PAY_FULL
        assert result.delta_amount == 0.0

    def test_gc24b_per_day_fee_with_grace_and_an_overstated_claim_is_pay_partial(self) -> None:
        """5 days - grace 2 = 3 chargeable x 50.00 = 150.00; claim 250.00 -> PAY_PARTIAL, delta 100.00."""
        rule = _rule("R-ASN-DAY", "ASN_LATE", CalcType.FLAT_FEE, 50.0, applies_per=APPLIES_PER_DAY)
        facts = _asn_facts(asn_offset=5, goods_issued_offset=0, delivered_offset=6, grace=2)

        result = recompute_dispute(rule, facts, 250.0)

        assert result.computed_amount == 150.0
        assert result.verdict == DisputeVerdict.PAY_PARTIAL
        assert result.delta_amount == 100.0

    @pytest.mark.parametrize(
        ("asn_offset", "grace", "delivered_offset"),
        [
            pytest.param(0, 0, 6, id="asn-same-day-as-goods-issue"),
            pytest.param(5, 5, 6, id="asn-late-but-inside-grace"),
            pytest.param(0, 0, 12, id="asn-on-time-delivery-late"),
        ],
    )
    def test_gc24c_asn_not_late_is_no_pay(self, asn_offset: int, grace: int, delivered_offset: int) -> None:
        """No chargeable ASN days -> NO_PAY, even when the truck itself arrived late (delivery date is ignored)."""
        rule = _rule("R-ASN", "ASN_LATE", CalcType.FLAT_FEE, 250.0)
        facts = _asn_facts(
            asn_offset=asn_offset, goods_issued_offset=0, delivered_offset=delivered_offset, grace=grace
        )

        result = recompute_dispute(rule, facts, 250.0)

        assert result.computed_amount == 0.0
        assert result.verdict == DisputeVerdict.NO_PAY
        assert result.delta_amount == 250.0


# --------------------------------------------------------------------------------------
# GC-25  Split shipment: dispute facts are aggregated across plans          (D6, D1)
# (service-level aggregation is not covered here; only its pricing consequence is)
# --------------------------------------------------------------------------------------


def test_gc25_split_shipment_facts_use_total_shipped_quantity_and_latest_delivery_date() -> None:
    """GC-25: PO 2,000 split 1,000 + 900 (delivered Mar 8, Mar 11). Facts: delivered_qty 1,900, date Mar 11."""
    shortage_facts = DisputeFacts(
        order_qty=2000,
        unit_price=20.00,
        delivered_qty=1000.0 + 900.0,
        required_delivery_date=_d(7),
        actual_delivery_date=None,
    )
    first_plan_only = DisputeFacts(
        order_qty=2000,
        unit_price=20.00,
        delivered_qty=1000.0,
        required_delivery_date=_d(7),
        actual_delivery_date=None,
    )
    latest_date = max(_d(6), _d(9))
    delay_facts = DisputeFacts(
        order_qty=2000,
        unit_price=20.00,
        delivered_qty=1900.0,
        required_delivery_date=_d(7),
        actual_delivery_date=latest_date,
    )
    earliest_date_facts = DisputeFacts(
        order_qty=2000,
        unit_price=20.00,
        delivered_qty=1900.0,
        required_delivery_date=_d(7),
        actual_delivery_date=_d(6),
    )

    aggregated = recompute_dispute(_fill_rate_floor(), shortage_facts, 60.0)
    partial_only = recompute_dispute(_fill_rate_floor(), first_plan_only, 60.0)
    on_latest = recompute_dispute(_otif_pct(), delay_facts, 1200.0)
    on_earliest = recompute_dispute(_otif_pct(), earliest_date_facts, 1200.0)

    assert aggregated.computed_amount == 60.0  # 100 short: 3% x (100 x 20.00)
    assert aggregated.verdict == DisputeVerdict.PAY_FULL
    assert partial_only.computed_amount == 600.0  # what the wrong (first-plan-only) quantity would claim
    assert on_latest.computed_amount == 1200.0  # latest actual DELIVERED date Mar 11 is 2 days late
    assert on_latest.verdict == DisputeVerdict.PAY_FULL
    assert on_earliest.computed_amount == 0.0  # the earliest date would wrongly clear the charge


# --------------------------------------------------------------------------------------
# Helpers (after the cases, in order of first use)
# --------------------------------------------------------------------------------------


def _d(offset: int) -> date:
    return D0 + timedelta(days=offset)


def _assess(
    plan: PlanTimelineInput,
    rules: Sequence[PenaltyRule],
    *,
    basis: PricingBasis = BASIS,
    shortfall: float = 0.0,
    shortfall_status: str | None = None,
    stacking: str = "SUM",
) -> PlanAssessment:
    return assess_plan(_definitions(), plan, shortfall, shortfall_status, basis, rules, stacking)


def _plan(
    *,
    freight_term: str = "PREPAID",
    milestones: tuple[MilestoneState, ...] | None = None,
    window_start: date | None = None,
    window_end: date | None = None,
    cancel_date: date | None = None,
    as_of: date = D0,
    not_before: Mapping[str, date] | None = None,
) -> PlanTimelineInput:
    return PlanTimelineInput(
        plan_id="PLAN-GC",
        freight_term=freight_term,
        planned_transit_days=2,
        window_start=window_start or _d(3),
        window_end=window_end or _d(7),
        cancel_date=cancel_date,
        milestones=milestones if milestones is not None else _states(freight_term),
        as_of=as_of,
        not_before=dict(not_before or {}),
    )


def _states(
    freight_term: str = "PREPAID",
    *,
    shifts: Mapping[str, int] | None = None,
    actuals: Mapping[str, int] | None = None,
) -> tuple[MilestoneState, ...]:
    """Baseline = planned = unshifted schedule; `shifts` moves planned dates; `actuals` marks done steps."""
    baseline = _PREPAID_BASELINE if freight_term == "PREPAID" else _COLLECT_BASELINE
    shifts = shifts or {}
    actuals = actuals or {}
    return tuple(
        MilestoneState(
            code=code,
            baseline_date=_d(offset),
            planned_date=_d(offset + shifts.get(code, 0)),
            actual_date=_d(actuals[code]) if code in actuals else None,
        )
        for code, offset in baseline.items()
    )


def _ms(
    code: str, *, baseline: date | None = None, planned: date | None = None, actual: date | None = None
) -> MilestoneState:
    return MilestoneState(code=code, baseline_date=baseline, planned_date=planned, actual_date=actual)


def _rule(
    rule_id: str,
    violation_type: str,
    calc_type: CalcType,
    rate: float = 0.0,
    *,
    threshold_pct: float = 0.0,
    cap_amount: float | None = None,
    basis_type: str | None = None,
    applies_per: str | None = None,
    grace_period_days: int = 0,
    tiers: list[PenaltyRuleTier] | None = None,
) -> PenaltyRule:
    return PenaltyRule(
        rule_id=rule_id,
        violation_type=violation_type,
        calc_type=calc_type,
        rate=rate,
        threshold_pct=threshold_pct,
        cap_amount=cap_amount,
        tiers=tiers,
        basis_type=basis_type,
        applies_per=applies_per,
        grace_period_days=grace_period_days,
    )


def _otif_pct(rate: float = 0.03, *, rule_id: str = "R-OTIF", grace: int = 0) -> PenaltyRule:
    """Retailer OTIF fee: a flat percentage of PO value once the delivery is late (past grace)."""
    return _rule(rule_id, "OTIF_LATE", CalcType.PERCENT_OF_PO, rate, grace_period_days=grace)


def _fill_rate_floor(rate: float = 0.03, floor: float = 0.98) -> PenaltyRule:
    """Retailer in-full rule: below the fill-rate floor, `rate` x shortfall value."""
    return _rule(
        "R-FILL",
        "SHORT_SHIP",
        CalcType.PERCENT_OF_PO,
        rate,
        threshold_pct=floor,
        basis_type="SHORTFALL_VALUE",
    )


def _short_per_unit(rate: float, *, threshold: float) -> PenaltyRule:
    return _rule("R-SHORT", "SHORT_SHIP", CalcType.PER_UNIT, rate, threshold_pct=threshold)


def _tiered_days_rule(application: str) -> PenaltyRule:
    bands = [(0.0, 3.0, 0.01), (3.0, 7.0, 0.02), (7.0, None, 0.04)]
    tiers = [
        PenaltyRuleTier(
            band_min=low, band_max=high, rate=rate, tier_application=application, tier_basis="DAYS_LATE"
        )
        for low, high, rate in bands
    ]
    return _rule("R-TIER", "OTIF_LATE", CalcType.TIERED, tiers=tiers)


def _priced(assessment: PlanAssessment, risk_type: str) -> PricedRisk:
    return next(p for p in assessment.priced_risks if p.risk_type == risk_type)


def _timing(assessment: PlanAssessment, risk_type: str) -> TimingRisk:
    return next(t for t in assessment.result.timing_risks if t.risk_type == risk_type)


def _demand(
    plan_id: str, line_id: str, quantity: float, *, need_offset: int, plan_number: str = "P1"
) -> SupplyDemand:
    return SupplyDemand(
        plan_id=plan_id,
        plan_line_id=line_id,
        quantity=quantity,
        need_date=_d(need_offset),
        order_date=D0,
        plan_number=plan_number,
    )


def _receipt(
    source_id: str,
    quantity: float,
    *,
    available_offset: int,
    baseline_offset: int | None,
    reason: str | None,
    source_type: str = "PRODUCTION_ORDER",
) -> SupplyReceipt:
    return SupplyReceipt(
        source_type=source_type,
        source_id=source_id,
        quantity=quantity,
        available_date=_d(available_offset),
        baseline_available_date=_d(baseline_offset) if baseline_offset is not None else None,
        reason_code=reason,
    )


def _single_outcome(
    *, on_hand: float, receipts: Sequence[SupplyReceipt], line_quantities: Sequence[float]
) -> PlanSupplyOutcome:
    """Allocate one plan's lines (all needed Mar 9) against one pool and roll up to the plan outcome."""
    demands = [
        _demand("PLAN-GC", f"LINE-{index}", quantity, need_offset=7)
        for index, quantity in enumerate(line_quantities, start=1)
    ]
    coverages = allocate_supply(on_hand=on_hand, receipts=receipts, demands=demands, as_of=D0)
    return plan_outcomes(coverages)[0]


def _make_reprice(rules: Sequence[PenaltyRule]):
    """Same contract as `RiskRowAssembler.make_reprice`: re-assess, stack PROJECTED_BREACH amounts."""

    def reprice(modified_plan: PlanTimelineInput, modified_shortfall: float) -> float:
        status = "PROJECTED_BREACH" if modified_shortfall > 1e-6 else None
        reassessed = _assess(
            modified_plan, rules, shortfall=modified_shortfall, shortfall_status=status, stacking="SUM"
        )
        amounts = [p.penalty_amount for p in reassessed.priced_risks if p.status == "PROJECTED_BREACH"]
        return stack_amounts(amounts, "SUM")

    return reprice


def _current(status: str, penalty: float, days_off: int) -> CurrentRisk:
    return CurrentRisk(
        risk_type="LATE", status=status, penalty_amount=penalty, days_off=days_off, shortfall_quantity=None
    )


def _alert_state(
    status: str,
    last_seen: date,
    *,
    prev: tuple[float | None, int | None],
    last: tuple[float, int],
) -> AlertState:
    return AlertState(
        alert_id="ALERT-GC",
        risk_type="LATE",
        status=status,
        last_seen_date=last_seen,
        prev_penalty_amount=prev[0],
        last_penalty_amount=last[0],
        prev_days_off=prev[1],
        last_days_off=last[1],
        prev_shortfall_quantity=None,
        last_shortfall_quantity=None,
    )


def _asn_facts(
    *, asn_offset: int, goods_issued_offset: int, delivered_offset: int, grace: int = 0
) -> DisputeFacts:
    """ASN facts for GC-24: the ASN is judged against goods issue, never against the delivery date."""
    return DisputeFacts(
        order_qty=2000,
        unit_price=20.00,
        delivered_qty=2000.0,
        required_delivery_date=_d(7),
        actual_delivery_date=_d(delivered_offset),
        grace_period_days=grace,
        asn_sent_date=_d(asn_offset),  # type: ignore[call-arg]
        goods_issued_date=_d(goods_issued_offset),  # type: ignore[call-arg]
    )
