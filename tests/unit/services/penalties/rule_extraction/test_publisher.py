"""Tests for the pure publication engine (`app.services.penalties.rule_extraction.publisher`):
the admission gate, the field mapping for all four supported calc types, tier
construction, and one case for each of the fifteen rejection reasons in
`penalty-rule-extraction.md` section 6.3.

Zero DB dependency -- no fixtures beyond the pure dataclasses.
"""

from datetime import date
from decimal import Decimal

from app.services.penalties.projection.types import CalcType, PenaltyRule, PenaltyRuleTier
from app.services.penalties.rule_extraction.publisher import PenaltyRulePublisher
from app.services.penalties.rule_extraction.types import (
    PublishedRule,
    RejectedPublication,
    RejectionReason,
    StagedFact,
    StagedRule,
)

RETAILER_CODE = "WMT"
CONTRACT_EFFECTIVE_DATE = date(2026, 1, 1)
FINGERPRINT = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"

PUBLISHER = PenaltyRulePublisher()


def _staged(
    calc_type: str = "PER_UNIT",
    facts: list[StagedFact] | None = None,
    status: str = "APPROVED",
    pricing_readiness: str = "READY",
    po_shortage_flag: bool = True,
    po_delay_flag: bool = False,
    penalty_category: str = "SHORT_SHIP",
) -> StagedRule:
    return StagedRule(
        id="extracted-rule-1",
        contract_id="contract-1",
        clause_fingerprint=FINGERPRINT,
        penalty_category=penalty_category,
        calc_type=calc_type,
        po_shortage_flag=po_shortage_flag,
        po_delay_flag=po_delay_flag,
        pricing_readiness=pricing_readiness,
        status=status,
        facts=facts or [],
    )


def _rate_fact(
    value: float | None,
    value_unit: str = "USD",
    branch_no: int = 0,
    basis_type: str | None = "PO_VALUE",
    applies_per: str | None = None,
    value_status: str = "PRESENT",
    currency_code: str | None = None,
) -> StagedFact:
    return StagedFact(
        branch_no=branch_no,
        attribute_role="RATE",
        value=Decimal(str(value)) if value is not None else None,
        value_unit=value_unit,
        value_status=value_status,
        basis_type=basis_type,
        applies_per=applies_per,
        currency_code=currency_code,
    )


def _threshold_fact(
    value: float | None,
    value_max: float | None = None,
    operator: str = "GTE",
    value_unit: str = "PERCENT",
    branch_no: int = 0,
    tier_application: str | None = None,
    metric_code: str | None = None,
    value_status: str = "PRESENT",
) -> StagedFact:
    return StagedFact(
        branch_no=branch_no,
        attribute_role="THRESHOLD",
        operator=operator,
        value=Decimal(str(value)) if value is not None else None,
        value_max=Decimal(str(value_max)) if value_max is not None else None,
        value_unit=value_unit,
        value_status=value_status,
        tier_application=tier_application,
        metric_code=metric_code,
    )


def _cap_fact(
    value: float | None,
    cap_scope: str = "AMOUNT_CEILING",
    branch_no: int = 0,
    currency_code: str | None = None,
) -> StagedFact:
    return StagedFact(
        branch_no=branch_no,
        attribute_role="CAP",
        value=Decimal(str(value)) if value is not None else None,
        cap_scope=cap_scope,
        currency_code=currency_code,
    )


def _grace_period_fact(
    value: float | None,
    value_unit: str = "CALENDAR_DAYS",
    branch_no: int = 0,
    value_status: str = "PRESENT",
) -> StagedFact:
    return StagedFact(
        branch_no=branch_no,
        attribute_role="GRACE_PERIOD",
        value=Decimal(str(value)) if value is not None else None,
        value_unit=value_unit,
        value_status=value_status,
    )


def _to_penalty_rule(result: PublishedRule) -> PenaltyRule:
    """Round-trips a `PublishedRule` into the pure projection dataclass it must feed."""
    return PenaltyRule(
        rule_id=result.rule_code,
        violation_type=result.violation_type,
        calc_type=CalcType(result.calc_type),
        rate=float(result.rate),
        threshold_pct=float(result.threshold_pct),
        cap_amount=float(result.cap_amount) if result.cap_amount is not None else None,
        tiers=[
            PenaltyRuleTier(band_min=float(t.band_min), band_max=float(t.band_max), rate=float(t.rate))
            for t in result.tiers
        ]
        or None,
    )


# ---------------------------------------------------------------------------
# Happy paths, one per supported calc_type
# ---------------------------------------------------------------------------


def test_publish_per_unit_happy_path():
    staged = _staged(calc_type="PER_UNIT", facts=[_rate_fact(2.5, value_unit="USD")])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    assert result.calc_type == "PER_UNIT"
    assert result.rate == Decimal("2.5")
    assert result.violation_type == "SHORT_SHIP"
    assert result.rule_code == f"{RETAILER_CODE}-SHORT_SHIP-{FINGERPRINT[:8]}"
    assert result.tiers == []


def test_publish_percent_of_po_happy_path_converts_percent_to_fraction():
    staged = _staged(calc_type="PERCENT_OF_PO", facts=[_rate_fact(2.5, value_unit="PERCENT")])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    assert result.rate == Decimal("0.025")


def test_publish_flat_fee_happy_path():
    staged = _staged(calc_type="FLAT_FEE", facts=[_rate_fact(500, value_unit="USD", basis_type="NONE")])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    assert result.calc_type == "FLAT_FEE"
    assert result.rate == Decimal(500)


def test_rejects_flat_fee_with_po_value_basis():
    staged = _staged(calc_type="FLAT_FEE", facts=[_rate_fact(500, value_unit="USD", basis_type="PO_VALUE")])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.UNSUPPORTED_BASIS


def test_publish_tiered_happy_path_builds_ascending_half_open_bands():
    facts = [
        _threshold_fact(0, branch_no=1),
        _rate_fact(1, value_unit="PERCENT", branch_no=1),
        _threshold_fact(10, branch_no=2),
        _rate_fact(2, value_unit="PERCENT", branch_no=2),
        _threshold_fact(20, branch_no=3),
        _rate_fact(3, value_unit="PERCENT", branch_no=3),
    ]
    staged = _staged(calc_type="TIERED", facts=facts)
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    bands = [(t.band_min, t.band_max, t.rate) for t in result.tiers]
    assert bands == [
        (Decimal(0), Decimal("0.1"), Decimal("0.01")),
        (Decimal("0.1"), Decimal("0.2"), Decimal("0.02")),
        (Decimal("0.2"), Decimal("Infinity"), Decimal("0.03")),
    ]


# ---------------------------------------------------------------------------
# Violation type mapping
# ---------------------------------------------------------------------------


def test_delay_flag_maps_to_otif_late_when_no_metric_names_a_family():
    staged = _staged(
        calc_type="FLAT_FEE",
        facts=[_rate_fact(10, value_unit="USD", basis_type="NONE")],
        po_shortage_flag=False,
        po_delay_flag=True,
        penalty_category="OTIF_LATE",
    )
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    assert result.violation_type == "OTIF_LATE"


def test_fill_rate_pct_metric_maps_to_fill_rate():
    facts = [
        _threshold_fact(0.02, metric_code="FILL_RATE_PCT"),
        _rate_fact(1, value_unit="PERCENT"),
    ]
    staged = _staged(calc_type="PERCENT_OF_PO", facts=facts, penalty_category="SHORT_SHIP")
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    assert result.violation_type == "FILL_RATE"


def test_fill_rate_pct_metric_wins_over_both_po_flags_true():
    facts = [
        _threshold_fact(0.02, metric_code="FILL_RATE_PCT"),
        _rate_fact(1, value_unit="PERCENT"),
    ]
    staged = _staged(
        calc_type="PERCENT_OF_PO",
        facts=facts,
        po_shortage_flag=True,
        po_delay_flag=True,
        penalty_category="FILL_RATE",
    )
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    assert result.violation_type == "FILL_RATE"


def test_otif_pct_metric_maps_to_otif_late():
    facts = [
        _threshold_fact(0.02, metric_code="OTIF_PCT"),
        _rate_fact(1, value_unit="PERCENT"),
    ]
    staged = _staged(calc_type="PERCENT_OF_PO", facts=facts, penalty_category="OTIF_LATE")
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    assert result.violation_type == "OTIF_LATE"


# ---------------------------------------------------------------------------
# Grace period accrual
# ---------------------------------------------------------------------------


def test_publish_grace_period_in_calendar_days():
    staged = _staged(
        calc_type="PER_UNIT",
        facts=[_rate_fact(2.5, value_unit="USD"), _grace_period_fact(5, value_unit="CALENDAR_DAYS")],
    )
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    assert result.grace_period_days == 5


def test_rejects_grace_period_in_weeks_as_unsupported_accrual():
    staged = _staged(
        calc_type="PER_UNIT",
        facts=[_rate_fact(2.5, value_unit="USD"), _grace_period_fact(1, value_unit="WEEKS")],
    )
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.UNSUPPORTED_ACCRUAL


# ---------------------------------------------------------------------------
# Determinism and PenaltyRule constructibility
# ---------------------------------------------------------------------------


def test_publish_is_deterministic():
    staged = _staged(calc_type="PER_UNIT", facts=[_rate_fact(2.5, value_unit="USD")])
    first = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    second = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert first == second
    assert isinstance(first, PublishedRule)
    assert first.rule_code == second.rule_code


def test_published_rule_constructs_valid_penalty_rule_for_tiered():
    facts = [
        _threshold_fact(0, branch_no=1),
        _rate_fact(1, value_unit="PERCENT", branch_no=1),
        _threshold_fact(10, branch_no=2),
        _rate_fact(2, value_unit="PERCENT", branch_no=2),
    ]
    staged = _staged(calc_type="TIERED", facts=facts)
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    rule = _to_penalty_rule(result)
    assert rule.calc_type == CalcType.TIERED
    assert rule.tiers is not None and len(rule.tiers) == 2


def test_published_rule_constructs_valid_penalty_rule_for_percent_of_po():
    staged = _staged(calc_type="PERCENT_OF_PO", facts=[_rate_fact(2.5, value_unit="PERCENT")])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    rule = _to_penalty_rule(result)
    assert rule.rate == 0.025
    assert rule.tiers is None


# ---------------------------------------------------------------------------
# Rejections, one per section 6.3 reason_code
# ---------------------------------------------------------------------------


def test_rejects_not_approved():
    staged = _staged(status="PENDING_REVIEW", facts=[_rate_fact(1, value_unit="USD")])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.NOT_APPROVED


def test_rejects_not_ready():
    staged = _staged(pricing_readiness="AWAITING_DATA", facts=[_rate_fact(1, value_unit="USD")])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.NOT_READY


def test_rejects_not_po_scoped():
    staged = _staged(po_shortage_flag=False, po_delay_flag=False, facts=[_rate_fact(1, value_unit="USD")])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.NOT_PO_SCOPED


def test_rejects_unsupported_calc_type():
    staged = _staged(calc_type="NON_MONETARY", facts=[_rate_fact(1, value_unit="USD")])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.UNSUPPORTED_CALC_TYPE


def test_rejects_percent_of_invoice():
    staged = _staged(calc_type="PERCENT_OF_INVOICE", facts=[_rate_fact(1, value_unit="PERCENT")])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.PERCENT_OF_INVOICE


def test_rejects_no_rate_value():
    staged = _staged(calc_type="PER_UNIT", facts=[])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.NO_RATE_VALUE


def test_rejects_marginal_tiers():
    facts = [
        _threshold_fact(0, branch_no=1, tier_application="MARGINAL"),
        _rate_fact(1, value_unit="PERCENT", branch_no=1),
    ]
    staged = _staged(calc_type="TIERED", facts=facts)
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.MARGINAL_TIERS


def test_rejects_non_half_open_tiers():
    facts = [
        _threshold_fact(0, branch_no=1, operator="LT"),
        _rate_fact(1, value_unit="PERCENT", branch_no=1),
    ]
    staged = _staged(calc_type="TIERED", facts=facts)
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.NON_HALF_OPEN_TIERS


def test_rejects_non_amount_cap():
    staged = _staged(
        calc_type="PER_UNIT",
        facts=[_rate_fact(1, value_unit="USD"), _cap_fact(100, cap_scope="RATE_CEILING")],
    )
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.NON_AMOUNT_CAP


def test_rejects_unsupported_basis():
    staged = _staged(
        calc_type="PER_UNIT", facts=[_rate_fact(1, value_unit="USD", basis_type="WHOLESALE_PRICE")]
    )
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.UNSUPPORTED_BASIS


def test_po_value_and_unit_cost_basis_map_onto_cost_of_goods_for_the_pricing_engine():
    # PO_VALUE and UNIT_COST are extraction-side basis values; the pricing engine
    # (`app.services.penalties.projection.types`) only understands COST_OF_GOODS and
    # SHORTFALL_VALUE. A published rule must carry a basis the engine recognizes, not
    # the raw extraction value, or it silently falls back to the wrong basis downstream.
    for basis_type in ("PO_VALUE", "UNIT_COST"):
        staged = _staged(
            calc_type="PERCENT_OF_PO", facts=[_rate_fact(2.5, value_unit="PERCENT", basis_type=basis_type)]
        )
        result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
        assert isinstance(result, PublishedRule)
        assert result.basis_type == "COST_OF_GOODS"


def test_rejects_shortfall_units_basis_as_unsupported_by_the_pricing_engine():
    # SHORTFALL_UNITS has no correct pricing-engine equivalent: mapping it onto
    # SHORTFALL_VALUE would multiply an already-per-unit rate by unit_price a second
    # time, so the rule must be rejected rather than published and mispriced.
    staged = _staged(
        calc_type="PERCENT_OF_PO", facts=[_rate_fact(2.5, value_unit="PERCENT", basis_type="SHORTFALL_UNITS")]
    )
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.UNSUPPORTED_BASIS


def test_rejects_unsupported_accrual():
    staged = _staged(calc_type="PER_UNIT", facts=[_rate_fact(1, value_unit="USD", applies_per="WEEK")])
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.UNSUPPORTED_ACCRUAL


def test_admits_per_day_accrual_since_the_pricing_engine_can_accrue_it():
    # `app.services.penalties.projection.delay.price_delay_penalty` accrues per day
    # only when `applies_per == APPLIES_PER_DAY` ("DAY"); this is the one duration
    # value the publisher must let through rather than reject.
    staged = _staged(
        calc_type="PERCENT_OF_PO",
        facts=[_rate_fact(4, value_unit="PERCENT", applies_per="DAY")],
        po_shortage_flag=False,
        po_delay_flag=True,
        penalty_category="OTIF_LATE",
    )
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, PublishedRule)
    assert result.applies_per == "DAY"


def test_rejects_per_month_accrual_as_unsupported_since_the_engine_cannot_accrue_it():
    # MONTH is in DURATION_APPLIES_PER but the pricing engine only accrues DAY;
    # admitting it would price a per-month clause as a single flat charge.
    staged = _staged(
        calc_type="PERCENT_OF_PO",
        facts=[_rate_fact(4, value_unit="PERCENT", applies_per="MONTH")],
        po_shortage_flag=False,
        po_delay_flag=True,
        penalty_category="OTIF_LATE",
    )
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.UNSUPPORTED_ACCRUAL


def test_rejects_threshold_out_of_range():
    facts = [
        _threshold_fact(150, branch_no=0, operator="GTE"),
        _rate_fact(1, value_unit="USD"),
    ]
    staged = _staged(calc_type="PER_UNIT", facts=facts)
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.THRESHOLD_OUT_OF_RANGE


def test_rejects_tier_band_gap():
    facts = [
        _threshold_fact(0, value_max=10, operator="BETWEEN", branch_no=1),
        _rate_fact(1, value_unit="PERCENT", branch_no=1),
        _threshold_fact(15, value_max=25, operator="BETWEEN", branch_no=2),
        _rate_fact(2, value_unit="PERCENT", branch_no=2),
    ]
    staged = _staged(calc_type="TIERED", facts=facts)
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.TIER_BAND_GAP


def test_rejects_external_figure():
    staged = _staged(
        calc_type="PER_UNIT",
        facts=[_rate_fact(None, value_unit="USD", value_status="EXTERNAL_REFERENCE")],
    )
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.EXTERNAL_FIGURE


def test_rejects_mixed_currency():
    facts = [
        _rate_fact(1, value_unit="USD", currency_code="USD"),
        _cap_fact(100, branch_no=0, currency_code="EUR"),
    ]
    staged = _staged(calc_type="PER_UNIT", facts=facts)
    result = PUBLISHER.publish(staged, RETAILER_CODE, CONTRACT_EFFECTIVE_DATE)
    assert isinstance(result, RejectedPublication)
    assert result.reason_code == RejectionReason.MIXED_CURRENCY
