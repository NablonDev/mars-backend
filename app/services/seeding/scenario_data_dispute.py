"""Dispute-resolution seed data: fixture rules and scenario descriptors, no repository access.

Eight dedicated purchase orders with their own fulfillment data cover every
verdict branch and error path; they never touch the four worked-example POs.
Three retailers isolate the rules from each other so that rule matching in
`PenaltyRuleRepository.list_rules_effective_on` is unambiguous per scenario:
`RET-DSPA` holds two ordinary effective rules, `RET-DSPB` the grace-period and
TIERED rules, `RET-DSPC` the lapsed rule.

Every `expected_*` field is hand-computed here and asserted against in
`tests/unit/services/test_dispute_seed_scenarios.py`, which carries the
worked arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

ViolationFamily = Literal["SHORTAGE", "DELAY"]


@dataclass(frozen=True)
class DisputeRuleFixture:
    """One `penalty_rule` row's seed fixture, isolated to a dedicated `RET-DSP*` retailer for dispute-scenario testing."""

    rule_code: str
    retailer_code: str
    violation_type: str
    calc_type: str
    rate: float
    threshold_pct: float = 0.0
    cap_amount: float | None = None
    grace_period_days: int = 0
    effective_start_date: date = date(2026, 1, 1)
    effective_end_date: date | None = None
    tiers: list[dict] | None = None


@dataclass(frozen=True)
class DisputeScenarioFixture:
    """One dedicated PO, its fulfillment facts, and one `actual_penalty` charge.

    The `expected_*` fields are what `DisputeResolutionService.analyze()` must produce for
    this scenario.
    """

    key: str
    description: str
    purchase_order_number: str
    retailer_code: str
    material_code: str
    plant_code: str
    order_qty: int
    unit_price: float
    order_date: date
    requested_delivery_date: date
    required_ship_date: date
    violation_type: str
    invoice_or_deduction_date: date
    claimed_amount: float
    # Real, final post-delivery facts. `delivered_qty=None` means "record no
    # delivery at all" (scenario e); `actual_delivery_date=None` means
    # "record no shipment at all".
    delivered_qty: float | None
    actual_delivery_date: date | None
    expected_verdict: str | None  # None when analyze() is expected to raise
    expected_error_code: str | None = None
    expected_computed_amount: float | None = None
    expected_delta_amount: float | None = None


RULES: list[DisputeRuleFixture] = [
    DisputeRuleFixture(
        rule_code="RULE-DSPA-SHORT",
        retailer_code="RET-DSPA",
        violation_type="SHORT_SHIP",
        calc_type="PER_UNIT",
        rate=5.0,
    ),
    DisputeRuleFixture(
        rule_code="RULE-DSPA-OTIF",
        retailer_code="RET-DSPA",
        violation_type="OTIF_LATE",
        calc_type="PER_UNIT",
        rate=3.0,
    ),
    DisputeRuleFixture(
        rule_code="RULE-DSPB-OTIFGRACE",
        retailer_code="RET-DSPB",
        violation_type="OTIF_LATE",
        calc_type="FLAT_FEE",
        rate=750.0,
        grace_period_days=3,
    ),
    DisputeRuleFixture(
        rule_code="RULE-DSPB-SHORTTIER",
        retailer_code="RET-DSPB",
        violation_type="SHORT_SHIP",
        calc_type="TIERED",
        rate=0.0,
        tiers=[
            {"band_min": 0.0, "band_max": 0.1, "rate": 0.01},
            {"band_min": 0.1, "band_max": 1.0, "rate": 0.05},
        ],
    ),
    DisputeRuleFixture(
        rule_code="RULE-DSPC-LAPSED",
        retailer_code="RET-DSPC",
        violation_type="SHORT_SHIP",
        calc_type="PER_UNIT",
        rate=4.0,
        effective_start_date=date(2025, 1, 1),
        effective_end_date=date(2025, 6, 30),
    ),
]

RETAILER_CODES = ["RET-DSPA", "RET-DSPB", "RET-DSPC"]
MATERIAL_CODE = "MAT-DSP"
PLANT_CODE = "PLANT-DSP"

SCENARIOS: list[DisputeScenarioFixture] = [
    DisputeScenarioFixture(
        key="correct_shortage",
        description="(a) Correct shortage charge: claimed matches computed within tolerance -> PAY_FULL.",
        purchase_order_number="ORD-DSP-A1",
        retailer_code="RET-DSPA",
        material_code=MATERIAL_CODE,
        plant_code=PLANT_CODE,
        order_qty=100,
        unit_price=10.0,
        order_date=date(2026, 5, 1),
        requested_delivery_date=date(2026, 6, 10),
        required_ship_date=date(2026, 6, 8),
        violation_type="SHORT_SHIP",
        invoice_or_deduction_date=date(2026, 6, 12),
        claimed_amount=50.0,  # shortfall 10 units x $5/unit = $50, matches exactly
        delivered_qty=90.0,
        actual_delivery_date=None,
        expected_verdict="PAY_FULL",
        expected_computed_amount=50.0,
        expected_delta_amount=0.0,
    ),
    DisputeScenarioFixture(
        key="overcharged_shortage",
        description="(b) Overcharged shortage: claimed > computed -> PAY_PARTIAL.",
        purchase_order_number="ORD-DSP-A2",
        retailer_code="RET-DSPA",
        material_code=MATERIAL_CODE,
        plant_code=PLANT_CODE,
        order_qty=100,
        unit_price=10.0,
        order_date=date(2026, 5, 1),
        requested_delivery_date=date(2026, 6, 10),
        required_ship_date=date(2026, 6, 8),
        violation_type="SHORT_SHIP",
        invoice_or_deduction_date=date(2026, 6, 12),
        claimed_amount=80.0,  # computed is $50, so the retailer overcharged by $30
        delivered_qty=90.0,
        actual_delivery_date=None,
        expected_verdict="PAY_PARTIAL",
        expected_computed_amount=50.0,
        expected_delta_amount=30.0,
    ),
    DisputeScenarioFixture(
        key="undercharged_delay",
        description="(c) Undercharged delay: claimed < computed -> PAY_FULL, negative delta_amount.",
        purchase_order_number="ORD-DSP-A3",
        retailer_code="RET-DSPA",
        material_code=MATERIAL_CODE,
        plant_code=PLANT_CODE,
        order_qty=100,
        unit_price=10.0,
        order_date=date(2026, 5, 1),
        requested_delivery_date=date(2026, 6, 10),
        required_ship_date=date(2026, 6, 8),
        violation_type="OTIF_LATE",
        invoice_or_deduction_date=date(2026, 6, 20),
        claimed_amount=200.0,  # computed is $300 (rate 3.0 x 100 units), so undercharged
        delivered_qty=100.0,
        actual_delivery_date=date(2026, 6, 15),  # 5 days late, no grace period on this rule
        expected_verdict="PAY_FULL",
        expected_computed_amount=300.0,
        expected_delta_amount=-100.0,
    ),
    DisputeScenarioFixture(
        key="no_matching_rule",
        description="(d) Charged violation type with no matching effective rule -> NO_MATCHING_RULE_FOR_DISPUTE.",
        purchase_order_number="ORD-DSP-A4",
        retailer_code="RET-DSPA",
        material_code=MATERIAL_CODE,
        plant_code=PLANT_CODE,
        order_qty=100,
        unit_price=10.0,
        order_date=date(2026, 5, 1),
        requested_delivery_date=date(2026, 6, 10),
        required_ship_date=date(2026, 6, 8),
        violation_type="ASN_LATE",  # RET-DSPA has no rule for this violation_type at all
        invoice_or_deduction_date=date(2026, 6, 12),
        claimed_amount=100.0,
        delivered_qty=None,
        actual_delivery_date=None,
        expected_verdict=None,
        expected_error_code="NO_MATCHING_RULE_FOR_DISPUTE",
    ),
    DisputeScenarioFixture(
        key="insufficient_data",
        description=(
            "(e) ActualPenalty on a PO with no delivery data recorded as-of the invoice date "
            "-> INSUFFICIENT_DATA_FOR_DISPUTE."
        ),
        purchase_order_number="ORD-DSP-A5",
        retailer_code="RET-DSPA",
        material_code=MATERIAL_CODE,
        plant_code=PLANT_CODE,
        order_qty=100,
        unit_price=10.0,
        order_date=date(2026, 5, 1),
        requested_delivery_date=date(2026, 6, 10),
        required_ship_date=date(2026, 6, 8),
        violation_type="SHORT_SHIP",
        invoice_or_deduction_date=date(2026, 6, 12),
        claimed_amount=50.0,
        delivered_qty=None,  # no delivery row recorded at all
        actual_delivery_date=None,
        expected_verdict=None,
        expected_error_code="INSUFFICIENT_DATA_FOR_DISPUTE",
    ),
    DisputeScenarioFixture(
        key="within_grace_period",
        description="(f) Delivered within the grace-period rule's window despite being charged -> NO_PAY.",
        purchase_order_number="ORD-DSP-B1",
        retailer_code="RET-DSPB",
        material_code=MATERIAL_CODE,
        plant_code=PLANT_CODE,
        order_qty=100,
        unit_price=10.0,
        order_date=date(2026, 5, 1),
        requested_delivery_date=date(2026, 6, 10),
        required_ship_date=date(2026, 6, 8),
        violation_type="OTIF_LATE",
        invoice_or_deduction_date=date(2026, 6, 15),
        claimed_amount=750.0,
        delivered_qty=None,
        actual_delivery_date=date(2026, 6, 12),  # 2 days late, within the rule's 3-day grace period
        expected_verdict="NO_PAY",
        expected_computed_amount=0.0,
        expected_delta_amount=750.0,
    ),
    DisputeScenarioFixture(
        key="tiered_shortage",
        description="(g) TIERED-shortage rule exercised end-to-end.",
        purchase_order_number="ORD-DSP-B2",
        retailer_code="RET-DSPB",
        material_code=MATERIAL_CODE,
        plant_code=PLANT_CODE,
        order_qty=200,
        unit_price=15.0,
        order_date=date(2026, 5, 1),
        requested_delivery_date=date(2026, 6, 10),
        required_ship_date=date(2026, 6, 8),
        violation_type="SHORT_SHIP",
        invoice_or_deduction_date=date(2026, 6, 12),
        # shortfall = 40 units -> gap_pct = 40/200 = 0.2 -> tier [0.1, 1.0) at
        # 5% of PO value: 0.05 x 200 x $15 = $150
        claimed_amount=150.0,
        delivered_qty=160.0,
        actual_delivery_date=None,
        expected_verdict="PAY_FULL",
        expected_computed_amount=150.0,
        expected_delta_amount=0.0,
    ),
    DisputeScenarioFixture(
        key="lapsed_rule",
        description=(
            "(h) A since-lapsed rule, still effective at the historical charge date: proves "
            "list_rules_effective_on includes a historically-active-but-now-lapsed rule."
        ),
        purchase_order_number="ORD-DSP-C1",
        retailer_code="RET-DSPC",
        material_code=MATERIAL_CODE,
        plant_code=PLANT_CODE,
        order_qty=50,
        unit_price=20.0,
        order_date=date(2025, 2, 1),
        requested_delivery_date=date(2025, 3, 10),
        required_ship_date=date(2025, 3, 8),
        violation_type="SHORT_SHIP",
        invoice_or_deduction_date=date(2025, 3, 15),  # within the rule's 2025-01-01..2025-06-30 window
        claimed_amount=20.0,  # shortfall 5 units x $4/unit = $20, matches exactly
        delivered_qty=45.0,
        actual_delivery_date=None,
        expected_verdict="PAY_FULL",
        expected_computed_amount=20.0,
        expected_delta_amount=0.0,
    ),
]
