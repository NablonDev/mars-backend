"""Exhaustive tests for the pure, framework-free dispute engine
(`app.services.penalties.dispute.engine`): every `calc_type` x both
violation families x all four classification branches, the missing-data
guard for each family, the grace-period boundary, and TIERED delay pricing.

Zero DB dependency -- no fixtures beyond the pure dataclasses.
"""

from datetime import date

import pytest

from app.services.penalties.dispute.engine import (
    ROUNDING_TOLERANCE,
    classify,
    compute_days_late,
    compute_deadline,
    compute_is_late,
    compute_shortfall_units,
    price_violation,
    recompute_dispute,
)
from app.services.penalties.dispute.types import (
    DisputeFacts,
    DisputeVerdict,
    InsufficientDataForDisputeError,
    UnsupportedDisputeCalcError,
)
from app.services.penalties.projection.types import APPLIES_PER_DAY, CalcType, PenaltyRule, PenaltyRuleTier

REQUIRED_DELIVERY = date(2026, 6, 10)


def _shortage_facts(order_qty: int, delivered_qty: float | None, unit_price: float = 10.0) -> DisputeFacts:
    return DisputeFacts(
        order_qty=order_qty,
        unit_price=unit_price,
        delivered_qty=delivered_qty,
        required_delivery_date=REQUIRED_DELIVERY,
        actual_delivery_date=None,
        grace_period_days=0,
    )


def _delay_facts(
    actual_delivery_date: date | None,
    grace_period_days: int = 0,
    order_qty: int = 100,
    unit_price: float = 10.0,
) -> DisputeFacts:
    return DisputeFacts(
        order_qty=order_qty,
        unit_price=unit_price,
        delivered_qty=order_qty,
        required_delivery_date=REQUIRED_DELIVERY,
        actual_delivery_date=actual_delivery_date,
        grace_period_days=grace_period_days,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_compute_shortfall_units_positive():
    facts = _shortage_facts(order_qty=100, delivered_qty=80)
    assert compute_shortfall_units(facts) == 20.0


def test_compute_shortfall_units_never_negative():
    facts = _shortage_facts(order_qty=100, delivered_qty=120)
    assert compute_shortfall_units(facts) == 0.0


def test_compute_deadline_adds_grace_period():
    facts = _delay_facts(actual_delivery_date=None, grace_period_days=3)
    assert compute_deadline(facts) == date(2026, 6, 13)


def test_compute_is_late_true_past_deadline():
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 15), grace_period_days=2)
    assert compute_is_late(facts) is True


def test_compute_is_late_false_within_deadline():
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 12), grace_period_days=2)
    assert compute_is_late(facts) is False


def test_compute_is_late_false_on_early_delivery():
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 5))
    assert compute_is_late(facts) is False


def test_compute_days_late_counts_from_grace_adjusted_deadline():
    # Deadline is 2026-06-12 (required + 2-day grace); delivered 5 days after that.
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 17), grace_period_days=2)
    assert compute_days_late(facts) == 5


def test_compute_days_late_floored_at_zero_on_early_delivery():
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 5))
    assert compute_days_late(facts) == 0


# ---------------------------------------------------------------------------
# classify() -- branch order and tolerance
# ---------------------------------------------------------------------------


def test_classify_no_pay_when_computed_is_zero_even_if_claimed_nonzero():
    verdict, delta = classify(0.0, 500.0)
    assert verdict is DisputeVerdict.NO_PAY
    assert delta == 500.0


def test_classify_no_pay_when_both_zero():
    verdict, delta = classify(0.0, 0.0)
    assert verdict is DisputeVerdict.NO_PAY
    assert delta == 0.0


def test_classify_pay_full_on_exact_match():
    verdict, delta = classify(100.0, 100.0)
    assert verdict is DisputeVerdict.PAY_FULL
    assert delta == 0.0


def test_classify_pay_full_within_rounding_tolerance():
    verdict, _delta = classify(100.0, 100.0 + ROUNDING_TOLERANCE)
    assert verdict is DisputeVerdict.PAY_FULL


def test_classify_pay_partial_when_overcharged():
    verdict, delta = classify(50.0, 80.0)
    assert verdict is DisputeVerdict.PAY_PARTIAL
    assert delta == 30.0


def test_classify_pay_full_on_undercharge_with_negative_delta():
    verdict, delta = classify(80.0, 50.0)
    assert verdict is DisputeVerdict.PAY_FULL
    assert delta == -30.0


# ---------------------------------------------------------------------------
# price_violation() -- missing-data guard, both families
# ---------------------------------------------------------------------------


def test_shortage_missing_delivered_qty_raises_insufficient_data():
    rule = PenaltyRule(rule_id="r1", violation_type="SHORT_SHIP", calc_type=CalcType.PER_UNIT, rate=5.0)
    facts = _shortage_facts(order_qty=100, delivered_qty=None)
    with pytest.raises(InsufficientDataForDisputeError):
        price_violation(rule, facts)


def test_delay_missing_actual_delivery_date_raises_insufficient_data():
    rule = PenaltyRule(rule_id="r1", violation_type="OTIF_LATE", calc_type=CalcType.PER_UNIT, rate=0.5)
    facts = _delay_facts(actual_delivery_date=None)
    with pytest.raises(InsufficientDataForDisputeError):
        price_violation(rule, facts)


# ---------------------------------------------------------------------------
# price_violation() -- shortage, every calc_type
# ---------------------------------------------------------------------------


def test_shortage_per_unit_penalized():
    rule = PenaltyRule(rule_id="r1", violation_type="SHORT_SHIP", calc_type=CalcType.PER_UNIT, rate=5.0)
    facts = _shortage_facts(order_qty=100, delivered_qty=90)
    amount, calc_trace = price_violation(rule, facts)
    assert amount == 50.0
    assert calc_trace["violation_family"] == "SHORTAGE"
    assert calc_trace["shortfall_units"] == 10.0


def test_shortage_per_unit_no_violation():
    rule = PenaltyRule(rule_id="r1", violation_type="SHORT_SHIP", calc_type=CalcType.PER_UNIT, rate=5.0)
    facts = _shortage_facts(order_qty=100, delivered_qty=100)
    amount, _ = price_violation(rule, facts)
    assert amount == 0.0


def test_shortage_percent_of_po_penalized():
    rule = PenaltyRule(rule_id="r1", violation_type="FILL_RATE", calc_type=CalcType.PERCENT_OF_PO, rate=0.02)
    facts = _shortage_facts(order_qty=100, delivered_qty=99, unit_price=10.0)
    amount, _ = price_violation(rule, facts)
    assert amount == pytest.approx(0.02 * 100 * 10.0)


def test_shortage_flat_fee_penalized():
    rule = PenaltyRule(rule_id="r1", violation_type="SHORT_SHIP", calc_type=CalcType.FLAT_FEE, rate=30.0)
    facts = _shortage_facts(order_qty=100, delivered_qty=95)
    amount, _ = price_violation(rule, facts)
    assert amount == 30.0


def test_shortage_tiered_penalized():
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="SHORT_SHIP",
        calc_type=CalcType.TIERED,
        tiers=[
            PenaltyRuleTier(band_min=0.0, band_max=0.1, rate=0.01),
            PenaltyRuleTier(band_min=0.1, band_max=1.0, rate=0.05),
        ],
    )
    facts = _shortage_facts(order_qty=100, delivered_qty=80, unit_price=10.0)  # gap_pct = 0.2
    amount, _ = price_violation(rule, facts)
    assert amount == pytest.approx(0.05 * 100 * 10.0)


def test_shortage_respects_threshold_pct():
    rule = PenaltyRule(
        rule_id="r1", violation_type="SHORT_SHIP", calc_type=CalcType.PER_UNIT, rate=5.0, threshold_pct=0.05
    )
    # 3% shortfall, under the 5% tolerance threshold -- no penalty at all.
    facts = _shortage_facts(order_qty=100, delivered_qty=97)
    amount, _ = price_violation(rule, facts)
    assert amount == 0.0


# ---------------------------------------------------------------------------
# price_violation() -- delay, every calc_type, grace period
# ---------------------------------------------------------------------------


def test_delay_per_unit_penalized():
    rule = PenaltyRule(rule_id="r1", violation_type="OTIF_LATE", calc_type=CalcType.PER_UNIT, rate=0.5)
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 15), order_qty=100)
    amount, calc_trace = price_violation(rule, facts)
    assert amount == 50.0
    assert calc_trace["violation_family"] == "DELAY"
    assert calc_trace["is_late"] is True


def test_delay_per_unit_applies_per_day_accrues_by_days_late():
    # 5 days late (2026-06-15 vs. deadline 2026-06-10); a flat per-unit price
    # would be 0.5 * 100 = 50.0, so a 250.0 result proves 5-day accrual, not
    # a single-period charge.
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="OTIF_LATE",
        calc_type=CalcType.PER_UNIT,
        rate=0.5,
        applies_per=APPLIES_PER_DAY,
    )
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 15), order_qty=100)
    amount, calc_trace = price_violation(rule, facts)
    assert amount == 250.0
    assert calc_trace["days_late"] == 5


def test_delay_percent_of_po_penalized():
    rule = PenaltyRule(rule_id="r1", violation_type="OTIF_LATE", calc_type=CalcType.PERCENT_OF_PO, rate=0.02)
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 15), order_qty=100, unit_price=10.0)
    amount, _ = price_violation(rule, facts)
    assert amount == pytest.approx(0.02 * 100 * 10.0)


def test_delay_flat_fee_penalized():
    rule = PenaltyRule(rule_id="r1", violation_type="ASN_LATE", calc_type=CalcType.FLAT_FEE, rate=75.0)
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 15))
    amount, _ = price_violation(rule, facts)
    assert amount == 75.0


def test_delay_respects_grace_period_within_window():
    rule = PenaltyRule(rule_id="r1", violation_type="OTIF_LATE", calc_type=CalcType.FLAT_FEE, rate=75.0)
    # 2 days late, 2-day grace period -- exactly at the deadline, not late.
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 12), grace_period_days=2)
    amount, calc_trace = price_violation(rule, facts)
    assert amount == 0.0
    assert calc_trace["is_late"] is False


def test_delay_one_day_beyond_grace_period_is_penalized():
    rule = PenaltyRule(rule_id="r1", violation_type="OTIF_LATE", calc_type=CalcType.FLAT_FEE, rate=75.0)
    # 3 days late, 2-day grace period -- one day past the deadline.
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 13), grace_period_days=2)
    amount, calc_trace = price_violation(rule, facts)
    assert amount == 75.0
    assert calc_trace["is_late"] is True


def test_delay_tiered_prices_via_days_late_bands():
    """TIERED delay pricing bands directly on days_late (tier_basis=DAYS_LATE), CLIFF
    lookup: [0, 5) at 1%, [5, None) at 3% of order value. Required 2026-06-10, actual
    2026-06-20, no grace period -> 10 days late, landing in the open top band:
    0.03 x (100 x $10.00) = $30."""
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="OTIF_LATE",
        calc_type=CalcType.TIERED,
        tiers=[
            PenaltyRuleTier(band_min=0, band_max=5, rate=0.01, tier_basis="DAYS_LATE"),
            PenaltyRuleTier(band_min=5, band_max=None, rate=0.03, tier_basis="DAYS_LATE"),
        ],
    )
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 20))
    amount, calc_trace = price_violation(rule, facts)
    assert amount == pytest.approx(30.0)
    assert calc_trace["violation_family"] == "DELAY"
    assert calc_trace["days_late"] == 10


def test_price_violation_unmapped_violation_type_raises_unsupported_dispute_calc():
    # A rule with no engine_family (published before Phase 1, or seeded directly) and a
    # violation_type outside both legacy sets has no dispatch family at all. A raw
    # ValueError would reach the API as an unhandled 500; UnsupportedDisputeCalcError is
    # caught by DisputeResolutionService.analyze and turned into a clean BusinessRuleError.
    rule = PenaltyRule(rule_id="r1", violation_type="QUALITY_DEFECT", calc_type=CalcType.FLAT_FEE, rate=1.0)
    facts = _shortage_facts(order_qty=100, delivered_qty=90)
    with pytest.raises(UnsupportedDisputeCalcError, match="not mapped"):
        price_violation(rule, facts)


def test_price_violation_unsupported_engine_family_raises_unsupported_dispute_calc():
    # An engine_family Phase 1 admits (e.g. LIABILITY_CAP) but Phase 4 has no dispatch
    # entry for yet: same fail-OPEN posture as an unrecognized violation_type.
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="LIABILITY_CAP",
        calc_type=CalcType.FLAT_FEE,
        rate=1.0,
        engine_family="LIABILITY_CAP",
    )
    facts = _shortage_facts(order_qty=100, delivered_qty=90)
    with pytest.raises(UnsupportedDisputeCalcError, match="not mapped"):
        price_violation(rule, facts)


# ---------------------------------------------------------------------------
# price_violation() -- Phase 4 family dispatch table
# ---------------------------------------------------------------------------


def _base_facts(**overrides) -> DisputeFacts:
    defaults = {
        "order_qty": 100,
        "unit_price": 10.0,
        "delivered_qty": 100.0,
        "required_delivery_date": REQUIRED_DELIVERY,
        "actual_delivery_date": None,
        "grace_period_days": 0,
    }
    defaults.update(overrides)
    return DisputeFacts(**defaults)


def test_quality_per_unit_recomputes_from_defect_units():
    """Acceptance criterion: a QUALITY_DEFECT_CHARGEBACK claim with defect_units and a
    PER_UNIT rule recomputes to defect_units * rate and classifies correctly."""
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="QUALITY_DEFECT",
        calc_type=CalcType.PER_UNIT,
        rate=4.0,
        engine_family="QUALITY",
    )
    facts = _base_facts(defect_units=12.0)
    amount, calc_trace = price_violation(rule, facts)
    assert amount == 48.0
    assert calc_trace["violation_family"] == "QUALITY"
    assert calc_trace["claim_supplied_keys"] == ["defect_units"]

    calculation = recompute_dispute(rule, facts, claimed_amount=48.0)
    assert calculation.verdict is DisputeVerdict.PAY_FULL


def test_quality_missing_defect_units_raises_insufficient_data():
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="QUALITY_DEFECT",
        calc_type=CalcType.PER_UNIT,
        rate=4.0,
        engine_family="QUALITY",
    )
    facts = _base_facts(defect_units=None)
    with pytest.raises(InsufficientDataForDisputeError):
        price_violation(rule, facts)


def test_quality_percent_of_po_uses_defect_rate_pct():
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="QUALITY_DEFECT",
        calc_type=CalcType.PERCENT_OF_PO,
        rate=0.05,
        engine_family="QUALITY",
    )
    facts = _base_facts(defect_rate_pct=0.2)
    amount, calc_trace = price_violation(rule, facts)
    assert amount == pytest.approx(0.05 * 100 * 10.0)
    assert calc_trace["claim_supplied_keys"] == ["defect_rate_pct"]


def test_cover_purchase_prices_the_markup_over_original_cost():
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="COVER_PURCHASE",
        calc_type=CalcType.PER_UNIT,
        rate=1.0,
        engine_family="COVER_PURCHASE",
    )
    facts = _base_facts(replacement_cost_paid=1300.0, original_cost=1000.0)
    amount, calc_trace = price_violation(rule, facts)
    assert amount == 300.0  # markup = 1300 - 1000, PER_UNIT at rate=1.0 is a dollar-for-dollar charge
    assert calc_trace["mars_derived_keys"] == ["original_cost"]


def test_cover_purchase_missing_replacement_cost_raises_insufficient_data():
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="COVER_PURCHASE",
        calc_type=CalcType.PER_UNIT,
        rate=1.0,
        engine_family="COVER_PURCHASE",
    )
    facts = _base_facts(original_cost=1000.0)
    with pytest.raises(InsufficientDataForDisputeError):
        price_violation(rule, facts)


def test_financial_generic_family_recomputes_from_occurrence_count():
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="FINANCIAL_ADJUSTMENT",
        calc_type=CalcType.PER_UNIT,
        rate=25.0,
        engine_family="FINANCIAL",
    )
    facts = _base_facts(occurrence_count=3.0)
    amount, calc_trace = price_violation(rule, facts)
    assert amount == 75.0
    assert calc_trace["claim_supplied_keys"] == ["occurrence_count"]


def test_storage_duration_fee_missing_storage_days_raises_insufficient_data():
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="STORAGE_DURATION",
        calc_type=CalcType.PER_UNIT,
        rate=2.0,
        engine_family="STORAGE_DURATION_FEE",
    )
    facts = _base_facts()
    with pytest.raises(InsufficientDataForDisputeError):
        price_violation(rule, facts)


def test_volume_commitment_prices_shortfall_and_ignores_nothing_from_claim():
    """VOLUME_COMMITMENT carries no claim_supplied_keys at all: both committed_quantity and
    actual_purchase_quantity are Mars-derived, populated by the service layer, never read
    from a claim (4f)."""
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="VOLUME_SHORTFALL",
        calc_type=CalcType.PER_UNIT,
        rate=2.0,
        engine_family="VOLUME_COMMITMENT",
        commitment_quantity=1000.0,
    )
    facts = _base_facts(committed_quantity=1000.0, actual_purchase_quantity=700.0)
    amount, calc_trace = price_violation(rule, facts)
    assert amount == 600.0  # (1000 - 700) shortfall units x $2/unit
    assert calc_trace["claim_supplied_keys"] == []
    assert calc_trace["mars_derived_keys"] == ["committed_quantity", "actual_purchase_quantity"]


def test_volume_commitment_missing_actual_purchase_quantity_raises_insufficient_data():
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="VOLUME_SHORTFALL",
        calc_type=CalcType.PER_UNIT,
        rate=2.0,
        engine_family="VOLUME_COMMITMENT",
        commitment_quantity=1000.0,
    )
    facts = _base_facts(committed_quantity=1000.0)
    with pytest.raises(InsufficientDataForDisputeError):
        price_violation(rule, facts)


# ---------------------------------------------------------------------------
# recompute_dispute() -- full pass, all four classification branches, both
# violation families
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delivered_qty", "claimed_amount", "expected_verdict", "expected_delta"),
    [
        (100, 500.0, DisputeVerdict.NO_PAY, 500.0),  # no real shortfall at all
        (90, 50.0, DisputeVerdict.PAY_FULL, 0.0),  # claim matches computed exactly
        (90, 80.0, DisputeVerdict.PAY_PARTIAL, 30.0),  # retailer overcharged
        (90, 20.0, DisputeVerdict.PAY_FULL, -30.0),  # retailer undercharged
    ],
)
def test_recompute_dispute_shortage_all_branches(
    delivered_qty, claimed_amount, expected_verdict, expected_delta
):
    rule = PenaltyRule(rule_id="r1", violation_type="SHORT_SHIP", calc_type=CalcType.PER_UNIT, rate=5.0)
    facts = _shortage_facts(order_qty=100, delivered_qty=delivered_qty)
    calculation = recompute_dispute(rule, facts, claimed_amount)
    assert calculation.verdict is expected_verdict
    assert calculation.delta_amount == expected_delta


@pytest.mark.parametrize(
    ("actual_delivery_date", "claimed_amount", "expected_verdict", "expected_delta"),
    [
        (date(2026, 6, 5), 500.0, DisputeVerdict.NO_PAY, 500.0),  # delivered early, no real violation
        (date(2026, 6, 15), 50.0, DisputeVerdict.PAY_FULL, 0.0),
        (date(2026, 6, 15), 80.0, DisputeVerdict.PAY_PARTIAL, 30.0),
        (date(2026, 6, 15), 20.0, DisputeVerdict.PAY_FULL, -30.0),
    ],
)
def test_recompute_dispute_delay_all_branches(
    actual_delivery_date, claimed_amount, expected_verdict, expected_delta
):
    rule = PenaltyRule(rule_id="r1", violation_type="OTIF_LATE", calc_type=CalcType.PER_UNIT, rate=0.5)
    facts = _delay_facts(actual_delivery_date=actual_delivery_date, order_qty=100)
    calculation = recompute_dispute(rule, facts, claimed_amount)
    assert calculation.verdict is expected_verdict
    assert calculation.delta_amount == expected_delta


_SHORTAGE_RATE_BY_CALC_TYPE = {
    CalcType.PER_UNIT: 5.0,
    CalcType.PERCENT_OF_PO: 0.02,  # a fraction, not a whole-number percent -- see PenaltyRule.__post_init__
    CalcType.FLAT_FEE: 30.0,
    CalcType.TIERED: 5.0,  # unused when tiers is set; PenaltyRule.rate is ignored for TIERED
}


@pytest.mark.parametrize(
    "calc_type", [CalcType.PER_UNIT, CalcType.PERCENT_OF_PO, CalcType.FLAT_FEE, CalcType.TIERED]
)
def test_recompute_dispute_shortage_covers_every_calc_type(calc_type):
    tiers = [PenaltyRuleTier(band_min=0.0, band_max=1.0, rate=0.05)] if calc_type == CalcType.TIERED else None
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="SHORT_SHIP",
        calc_type=calc_type,
        rate=_SHORTAGE_RATE_BY_CALC_TYPE[calc_type],
        tiers=tiers,
    )
    facts = _shortage_facts(order_qty=100, delivered_qty=90)
    calculation = recompute_dispute(rule, facts, claimed_amount=1000.0)
    assert calculation.computed_amount >= 0.0
    assert calculation.verdict is DisputeVerdict.PAY_PARTIAL


_DELAY_RATE_BY_CALC_TYPE = {
    CalcType.PER_UNIT: 5.0,
    CalcType.PERCENT_OF_PO: 0.02,
    CalcType.FLAT_FEE: 30.0,
}


@pytest.mark.parametrize("calc_type", [CalcType.PER_UNIT, CalcType.PERCENT_OF_PO, CalcType.FLAT_FEE])
def test_recompute_dispute_delay_covers_every_supported_calc_type(calc_type):
    rule = PenaltyRule(
        rule_id="r1",
        violation_type="OTIF_LATE",
        calc_type=calc_type,
        rate=_DELAY_RATE_BY_CALC_TYPE[calc_type],
    )
    facts = _delay_facts(actual_delivery_date=date(2026, 6, 20), order_qty=100)
    calculation = recompute_dispute(rule, facts, claimed_amount=1000.0)
    assert calculation.computed_amount >= 0.0
    assert calculation.verdict is DisputeVerdict.PAY_PARTIAL


def test_recompute_dispute_respects_cap_amount():
    rule = PenaltyRule(
        rule_id="r1", violation_type="SHORT_SHIP", calc_type=CalcType.PER_UNIT, rate=5.0, cap_amount=10.0
    )
    facts = _shortage_facts(order_qty=100, delivered_qty=50)  # uncapped would be 250
    calculation = recompute_dispute(rule, facts, claimed_amount=10.0)
    assert calculation.computed_amount == 10.0
    assert calculation.verdict is DisputeVerdict.PAY_FULL
    assert calculation.calc_trace["cap_applied"] is True
