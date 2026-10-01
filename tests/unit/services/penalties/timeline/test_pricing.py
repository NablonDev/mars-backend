"""Tests for the pure pricing adapter."""

import pytest

from app.services.penalties.projection import CalcType, PenaltyRule
from app.services.penalties.timeline.pricing import PricingBasis, price_risk

BASIS = PricingBasis(quantity=100.0, unit_cost=10.0, unit_price=20.0)


def _rule(
    rule_id: str,
    violation_type: str,
    calc_type: CalcType = CalcType.PER_UNIT,
    rate: float = 1.0,
    basis_type: str | None = None,
    grace_period_days: int = 0,
) -> PenaltyRule:
    return PenaltyRule(
        rule_id=rule_id,
        violation_type=violation_type,
        calc_type=calc_type,
        rate=rate,
        basis_type=basis_type,
        grace_period_days=grace_period_days,
    )


def test_late_prices_against_otif_late_rule():
    rule = _rule("R1", "OTIF_LATE", rate=2.0)
    result = price_risk("LATE", "PROJECTED_BREACH", 3, None, BASIS, [rule], "SUM")

    assert result.penalty_amount == 200.0  # PER_UNIT: rate(2) * qty(100)
    assert result.priced_rule_ids == ("R1",)
    assert result.status == "PROJECTED_BREACH"


def test_late_falls_back_to_delivery_window_violation_when_no_otif_rule():
    rule = _rule("R2", "DELIVERY_WINDOW_VIOLATION", rate=1.0)
    result = price_risk("LATE", "PROJECTED_BREACH", 2, None, BASIS, [rule], "SUM")

    assert result.priced_rule_ids == ("R2",)
    assert result.penalty_amount > 0


def test_late_prefers_otif_late_rule_over_delivery_window_violation_when_both_exist():
    otif = _rule("R2A", "OTIF_LATE", rate=1.0)
    window = _rule("R2B", "DELIVERY_WINDOW_VIOLATION", rate=1.0)
    result = price_risk("LATE", "PROJECTED_BREACH", 2, None, BASIS, [otif, window], "SUM")

    assert result.priced_rule_ids == ("R2A",)


def test_early_prices_against_delivery_window_violation_rule():
    rule = _rule("R3", "DELIVERY_WINDOW_VIOLATION", rate=1.0)
    result = price_risk("EARLY", "PROJECTED_BREACH", -2, None, BASIS, [rule], "SUM")

    assert result.priced_rule_ids == ("R3",)


def test_asn_late_prices_against_asn_late_rule():
    rule = _rule("R4", "ASN_LATE", rate=1.0)
    result = price_risk("ASN_LATE", "PROJECTED_BREACH", 1, None, BASIS, [rule], "SUM")

    assert result.priced_rule_ids == ("R4",)


def test_short_prices_against_short_ship_and_fill_rate_rules():
    short_ship = _rule("R5", "SHORT_SHIP", rate=1.0)
    fill_rate = _rule("R6", "FILL_RATE", rate=1.0)
    result = price_risk("SHORT", "PROJECTED_BREACH", None, 10.0, BASIS, [short_ship, fill_rate], "SUM")

    assert set(result.priced_rule_ids) == {"R5", "R6"}
    assert result.penalty_amount == 20.0  # 10 shortfall units * rate(1) per rule, summed


def test_not_delivered_prices_full_quantity_as_shortfall():
    rule = _rule("R7", "SHORT_SHIP", rate=1.0)
    with_full = price_risk("NOT_DELIVERED", "PROJECTED_BREACH", None, None, BASIS, [rule], "SUM")
    with_partial = price_risk("SHORT", "PROJECTED_BREACH", None, 10.0, BASIS, [rule], "SUM")

    assert with_full.penalty_amount > with_partial.penalty_amount
    assert with_full.penalty_amount == 100.0  # full basis.quantity(100) * rate(1)


def test_grace_period_absorbs_a_one_day_late_and_downgrades_projected_breach_to_slipping():
    rule = _rule("R8", "OTIF_LATE", rate=5.0, grace_period_days=2)
    result = price_risk("LATE", "PROJECTED_BREACH", 1, None, BASIS, [rule], "SUM")

    assert result.penalty_amount == 0.0
    assert result.status == "SLIPPING"
    assert result.priced_rule_ids == ()


def test_grace_absorbed_zero_price_does_not_downgrade_an_already_breached_status():
    rule = _rule("R9", "OTIF_LATE", rate=5.0, grace_period_days=2)
    result = price_risk("LATE", "BREACHED", 1, None, BASIS, [rule], "SUM")

    assert result.penalty_amount == 0.0
    assert result.status == "BREACHED"


def test_unit_amount_uses_unit_price_for_po_value_basis_type():
    rule = _rule("R10", "OTIF_LATE", calc_type=CalcType.PERCENT_OF_PO, rate=1.0, basis_type="PO_VALUE")
    basis = PricingBasis(quantity=10.0, unit_cost=1.0, unit_price=100.0)
    result = price_risk("LATE", "PROJECTED_BREACH", 1, None, basis, [rule], "SUM")

    assert result.penalty_amount == 1000.0  # rate(1) * qty(10) * unit_price(100)


def test_unit_amount_falls_back_to_unit_price_when_unit_cost_is_none():
    rule = _rule("R11", "OTIF_LATE", calc_type=CalcType.PERCENT_OF_PO, rate=1.0)
    basis = PricingBasis(quantity=10.0, unit_cost=None, unit_price=50.0)
    result = price_risk("LATE", "PROJECTED_BREACH", 1, None, basis, [rule], "SUM")

    assert result.penalty_amount == 500.0  # rate(1) * qty(10) * unit_price(50), no unit_cost available


def test_unit_amount_ignores_standard_cost_whatever_the_basis_type():
    # A retailer's "cost of goods" is what it pays Mars (the PO price); Mars's own
    # standard cost must never change a penalty.
    basis = PricingBasis(quantity=10.0, unit_cost=8.0, unit_price=50.0)
    for basis_type in (None, "COST_OF_GOODS", "PO_VALUE"):
        rule = _rule("R12", "OTIF_LATE", calc_type=CalcType.PERCENT_OF_PO, rate=1.0, basis_type=basis_type)
        result = price_risk("LATE", "PROJECTED_BREACH", 1, None, basis, [rule], "SUM")

        assert result.penalty_amount == 500.0  # rate(1) * qty(10) * unit_price(50)


def test_stacking_mode_sum_adds_all_priced_rules():
    rule_a = _rule("RA", "OTIF_LATE", calc_type=CalcType.PERCENT_OF_PO, rate=1.0)
    rule_b = _rule("RB", "OTIF_LATE", calc_type=CalcType.PERCENT_OF_PO, rate=2.0)
    result = price_risk("LATE", "PROJECTED_BREACH", 1, None, BASIS, [rule_a, rule_b], "SUM")

    assert result.penalty_amount == 6000.0  # (1*100*20) + (2*100*20), PO unit price


def test_stacking_mode_max_takes_the_larger_priced_rule():
    rule_a = _rule("RA", "OTIF_LATE", calc_type=CalcType.PERCENT_OF_PO, rate=1.0)
    rule_b = _rule("RB", "OTIF_LATE", calc_type=CalcType.PERCENT_OF_PO, rate=2.0)
    result = price_risk("LATE", "PROJECTED_BREACH", 1, None, BASIS, [rule_a, rule_b], "MAX")

    assert result.penalty_amount == 4000.0


def test_unrecognized_stacking_mode_raises_value_error():
    rule = _rule("RC", "OTIF_LATE", rate=1.0)
    with pytest.raises(ValueError):
        price_risk("LATE", "PROJECTED_BREACH", 1, None, BASIS, [rule], "AVERAGE")


def test_priced_rule_ids_excludes_zero_amount_rules():
    zero_rule = _rule("RZ", "OTIF_LATE", rate=0.0)
    nonzero_rule = _rule("RN", "OTIF_LATE", rate=1.0)
    result = price_risk("LATE", "PROJECTED_BREACH", 1, None, BASIS, [zero_rule, nonzero_rule], "SUM")

    assert result.priced_rule_ids == ("RN",)


def test_rule_breakdown_has_one_entry_per_considered_rule_for_a_timing_risk():
    rule = _rule("R1", "OTIF_LATE", rate=2.0)
    result = price_risk("LATE", "PROJECTED_BREACH", 3, None, BASIS, [rule], "SUM")

    assert len(result.rule_breakdown) == 1
    charge = result.rule_breakdown[0]
    assert charge.rule_id == "R1"
    assert charge.rule_code is None  # pure module never resolves rule_code
    assert charge.violation_type == "OTIF_LATE"
    assert charge.calc_type == "PER_UNIT"
    assert charge.rate == 2.0
    assert charge.basis_type is None
    assert charge.unit_amount == 20.0  # PO unit price, never the cheaper unit_cost
    assert charge.quantity == 100.0
    assert charge.shortfall_quantity is None
    assert charge.days_off == 3
    assert charge.grace_period_days == 0
    assert charge.chargeable_days == 3
    assert charge.amount == 200.0


def test_rule_breakdown_includes_a_grace_absorbed_rule_with_zero_amount():
    rule = _rule("R8", "OTIF_LATE", rate=5.0, grace_period_days=2)
    result = price_risk("LATE", "PROJECTED_BREACH", 1, None, BASIS, [rule], "SUM")

    assert len(result.rule_breakdown) == 1
    charge = result.rule_breakdown[0]
    assert charge.chargeable_days == 0
    assert charge.amount == 0.0


def test_rule_breakdown_uses_fallback_basis_and_reports_shortfall_for_shortage_risk():
    rule = _rule("R5", "SHORT_SHIP", rate=1.0)
    basis = PricingBasis(quantity=100.0, unit_cost=None, unit_price=20.0)
    result = price_risk("SHORT", "PROJECTED_BREACH", None, 10.0, basis, [rule], "SUM")

    assert len(result.rule_breakdown) == 1
    charge = result.rule_breakdown[0]
    assert charge.unit_amount == 20.0  # falls back to unit_price when unit_cost is None
    assert charge.shortfall_quantity == 10.0
    assert charge.days_off is None
    assert charge.chargeable_days is None
    assert charge.amount == 10.0


def test_breach_with_no_matching_rule_says_so_instead_of_reading_as_a_forgiven_fine():
    # A retailer whose contract has no ASN clause: the breach is real but nothing can price it.
    unrelated = _rule("R-OTIF", "OTIF_LATE", rate=2.0)
    result = price_risk("ASN_LATE", "PROJECTED_BREACH", 5, None, BASIS, [unrelated], "SUM")

    assert result.penalty_amount == 0.0
    assert result.status == "SLIPPING"
    assert result.rule_breakdown == ()
    assert result.zero_reason == "NO_APPLICABLE_RULE"


def test_breach_inside_grace_is_a_different_zero_than_no_rule():
    rule = _rule("R-GRACE", "OTIF_LATE", rate=2.0, grace_period_days=3)
    result = price_risk("LATE", "PROJECTED_BREACH", 2, None, BASIS, [rule], "SUM")

    assert result.penalty_amount == 0.0
    assert result.status == "SLIPPING"
    assert len(result.rule_breakdown) == 1
    assert result.zero_reason == "WITHIN_GRACE_OR_THRESHOLD"


def test_a_charged_breach_has_no_zero_reason():
    result = price_risk("LATE", "PROJECTED_BREACH", 3, None, BASIS, [_rule("R1", "OTIF_LATE")], "SUM")

    assert result.penalty_amount > 0
    assert result.zero_reason is None


def test_breached_with_no_rule_stays_breached_but_still_says_no_rule():
    result = price_risk("NOT_DELIVERED", "BREACHED", 4, None, BASIS, [], "SUM")

    assert result.status == "BREACHED"
    assert result.zero_reason == "NO_APPLICABLE_RULE"
