"""Tests for the pure supply-position allocation engine."""

from datetime import date, timedelta

from app.services.penalties.timeline.supply import (
    SupplyDemand,
    SupplyReceipt,
    allocate_supply,
    plan_outcomes,
)

DAY0 = date(2026, 1, 1)


def _demand(
    plan_id: str,
    plan_line_id: str,
    quantity: float,
    need_date: date,
    order_date: date = DAY0,
    plan_number: str = "P1",
) -> SupplyDemand:
    return SupplyDemand(
        plan_id=plan_id,
        plan_line_id=plan_line_id,
        quantity=quantity,
        need_date=need_date,
        order_date=order_date,
        plan_number=plan_number,
    )


def _receipt(
    source_id: str,
    quantity: float,
    available_date: date,
    source_type: str = "PRODUCTION_ORDER",
    baseline_available_date: date | None = None,
    reason_code: str | None = None,
) -> SupplyReceipt:
    return SupplyReceipt(
        source_type=source_type,
        source_id=source_id,
        quantity=quantity,
        available_date=available_date,
        baseline_available_date=baseline_available_date,
        reason_code=reason_code,
    )


def test_on_hand_covers_all_demand() -> None:
    demand = _demand("PLAN-1", "LINE-1", 100.0, need_date=DAY0 + timedelta(days=5))

    coverages = allocate_supply(on_hand=150.0, receipts=[], demands=[demand], as_of=DAY0)

    assert len(coverages) == 1
    coverage = coverages[0]
    assert coverage.on_time_quantity == 100.0
    assert coverage.uncovered_quantity == 0.0
    assert coverage.full_cover_date == demand.need_date
    assert coverage.cause_code is None
    assert coverage.cause_source_id is None


def test_earlier_demand_wins_and_later_demand_is_short() -> None:
    early = _demand("PLAN-1", "LINE-1", 80.0, need_date=DAY0 + timedelta(days=5))
    late = _demand("PLAN-2", "LINE-1", 80.0, need_date=DAY0 + timedelta(days=10))

    coverages = allocate_supply(on_hand=100.0, receipts=[], demands=[late, early], as_of=DAY0)
    by_plan = {c.plan_id: c for c in coverages}

    assert by_plan["PLAN-1"].on_time_quantity == 80.0
    assert by_plan["PLAN-1"].uncovered_quantity == 0.0

    assert by_plan["PLAN-2"].on_time_quantity == 20.0
    assert by_plan["PLAN-2"].uncovered_quantity == 60.0
    assert by_plan["PLAN-2"].full_cover_date is None
    assert by_plan["PLAN-2"].cause_code == "DEMAND_EXCEEDS_SUPPLY"
    assert by_plan["PLAN-2"].cause_source_id is None


def test_delayed_qa_lot_receipt_with_baseline_before_need_gives_its_reason_as_cause() -> None:
    need_date = DAY0 + timedelta(days=10)
    demand = _demand("PLAN-1", "LINE-1", 100.0, need_date=need_date)
    receipt = _receipt(
        "QA-1",
        60.0,
        available_date=need_date + timedelta(days=3),
        source_type="QA_LOT",
        baseline_available_date=need_date - timedelta(days=2),
        reason_code="LAB_HOLD",
    )

    coverages = allocate_supply(on_hand=40.0, receipts=[receipt], demands=[demand], as_of=DAY0)
    coverage = coverages[0]

    assert coverage.on_time_quantity == 40.0
    assert coverage.uncovered_quantity == 0.0
    assert coverage.full_cover_date == receipt.available_date
    assert coverage.cause_code == "LAB_HOLD"
    assert coverage.cause_source_id == "QA-1"

    outcomes = plan_outcomes(coverages)
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.shortfall_quantity == 60.0
    assert outcome.material_available_not_before is None
    assert outcome.cause_code == "LAB_HOLD"
    assert outcome.cause_source_id == "QA-1"
    assert outcome.full_cover_date == receipt.available_date


def test_zero_on_hand_with_later_receipt_gives_wait_policy() -> None:
    need_date = DAY0 + timedelta(days=5)
    receipt_date = DAY0 + timedelta(days=8)
    demand = _demand("PLAN-1", "LINE-1", 50.0, need_date=need_date)
    receipt = _receipt("PO-1", 50.0, available_date=receipt_date)

    coverages = allocate_supply(on_hand=0.0, receipts=[receipt], demands=[demand], as_of=DAY0)
    outcomes = plan_outcomes(coverages)
    outcome = outcomes[0]

    assert outcome.shortfall_quantity == 0.0
    assert outcome.material_available_not_before == receipt_date
    assert outcome.full_cover_date == receipt_date


def test_no_supply_at_all_gives_uncovered_shortfall() -> None:
    demand = _demand("PLAN-1", "LINE-1", 30.0, need_date=DAY0 + timedelta(days=5))

    coverages = allocate_supply(on_hand=0.0, receipts=[], demands=[demand], as_of=DAY0)
    coverage = coverages[0]

    assert coverage.on_time_quantity == 0.0
    assert coverage.uncovered_quantity == 30.0
    assert coverage.full_cover_date is None
    assert coverage.cause_code == "DEMAND_EXCEEDS_SUPPLY"

    outcomes = plan_outcomes(coverages)
    outcome = outcomes[0]
    assert outcome.shortfall_quantity == 30.0
    assert outcome.material_available_not_before is None
    assert outcome.full_cover_date is None


def test_demand_tie_break_ordering_is_deterministic_by_plan_number() -> None:
    need_date = DAY0 + timedelta(days=5)
    receipt_b = _receipt("SRC-B", 10.0, available_date=DAY0)
    receipt_a = _receipt("SRC-A", 10.0, available_date=DAY0)
    demand_b = _demand("PLAN-B", "LINE-1", 5.0, need_date=need_date, plan_number="B")
    demand_a = _demand("PLAN-A", "LINE-1", 5.0, need_date=need_date, plan_number="A")

    coverages = allocate_supply(
        on_hand=0.0, receipts=[receipt_b, receipt_a], demands=[demand_b, demand_a], as_of=DAY0
    )

    assert [c.plan_id for c in coverages] == ["PLAN-A", "PLAN-B"]
    assert coverages[0].on_time_quantity == 5.0
    assert coverages[1].on_time_quantity == 5.0


def test_bucket_tie_break_by_source_id_determines_which_receipt_wins_the_cause() -> None:
    need_date = DAY0 + timedelta(days=5)
    late_date = need_date + timedelta(days=2)
    receipt_a = _receipt(
        "SRC-A",
        4.0,
        available_date=late_date,
        baseline_available_date=need_date - timedelta(days=1),
        reason_code="REASON_A",
    )
    receipt_b = _receipt(
        "SRC-B",
        6.0,
        available_date=late_date,
        baseline_available_date=need_date - timedelta(days=1),
        reason_code="REASON_B",
    )
    demand = _demand("PLAN-1", "LINE-1", 10.0, need_date=need_date)

    # Receipts passed in reverse alphabetical order to prove the outcome comes
    # from the (date, source_id) sort, not from input order.
    coverages = allocate_supply(on_hand=0.0, receipts=[receipt_b, receipt_a], demands=[demand], as_of=DAY0)
    coverage = coverages[0]

    assert coverage.on_time_quantity == 0.0
    assert coverage.uncovered_quantity == 0.0
    assert coverage.full_cover_date == late_date
    assert coverage.cause_code == "REASON_A"
    assert coverage.cause_source_id == "SRC-A"


def test_delayed_receipt_cause_survives_a_higher_priority_demand_exhausting_it() -> None:
    need_date = DAY0 + timedelta(days=10)
    qa_receipt = _receipt(
        "QA-1",
        5.0,
        available_date=need_date + timedelta(days=3),
        source_type="QA_LOT",
        baseline_available_date=need_date - timedelta(days=2),
        reason_code="LAB_HOLD",
    )
    high_priority = _demand("PLAN-1", "LINE-1", 5.0, need_date=need_date, plan_number="A")
    low_priority = _demand("PLAN-2", "LINE-1", 5.0, need_date=need_date, plan_number="B")

    coverages = allocate_supply(
        on_hand=0.0, receipts=[qa_receipt], demands=[high_priority, low_priority], as_of=DAY0
    )
    by_plan = {c.plan_id: c for c in coverages}

    assert by_plan["PLAN-1"].on_time_quantity == 0.0
    assert by_plan["PLAN-1"].uncovered_quantity == 0.0
    assert by_plan["PLAN-1"].full_cover_date == qa_receipt.available_date
    assert by_plan["PLAN-1"].cause_code == "LAB_HOLD"
    assert by_plan["PLAN-1"].cause_source_id == "QA-1"

    assert by_plan["PLAN-2"].on_time_quantity == 0.0
    assert by_plan["PLAN-2"].uncovered_quantity == 5.0
    assert by_plan["PLAN-2"].full_cover_date is None
    assert by_plan["PLAN-2"].cause_code == "LAB_HOLD"
    assert by_plan["PLAN-2"].cause_source_id == "QA-1"


def test_full_cover_date_when_fully_on_time_equals_need_date_even_with_extra_early_bucket() -> None:
    need_date = DAY0 + timedelta(days=5)
    demand = _demand("PLAN-1", "LINE-1", 20.0, need_date=need_date)
    receipt = _receipt("PO-1", 20.0, available_date=DAY0)

    coverages = allocate_supply(on_hand=0.0, receipts=[receipt], demands=[demand], as_of=DAY0)
    coverage = coverages[0]

    assert coverage.full_cover_date == need_date


def test_ship_available_policy_when_some_lines_on_time_and_some_short() -> None:
    need_date = DAY0 + timedelta(days=5)
    line_1 = _demand("PLAN-1", "LINE-1", 10.0, need_date=need_date)
    line_2 = _demand("PLAN-1", "LINE-2", 20.0, need_date=need_date)

    coverages = allocate_supply(on_hand=10.0, receipts=[], demands=[line_1, line_2], as_of=DAY0)
    outcomes = plan_outcomes(coverages)
    outcome = outcomes[0]

    assert outcome.shortfall_quantity == 20.0
    assert outcome.material_available_not_before is None
    assert outcome.cause_code == "DEMAND_EXCEEDS_SUPPLY"


def test_plan_outcomes_fully_on_time() -> None:
    need_date = DAY0 + timedelta(days=5)
    demand = _demand("PLAN-1", "LINE-1", 20.0, need_date=need_date)

    coverages = allocate_supply(on_hand=20.0, receipts=[], demands=[demand], as_of=DAY0)
    outcomes = plan_outcomes(coverages)
    outcome = outcomes[0]

    assert outcome.shortfall_quantity == 0.0
    assert outcome.material_available_not_before is None
    assert outcome.cause_code is None
    assert outcome.cause_source_id is None
    assert outcome.full_cover_date == need_date
