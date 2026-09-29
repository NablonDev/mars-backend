"""Unit tests for app/services/fine_mitigation/ (pure engine) and
app/repositories/fine_mitigation/mitigation.py (DB round-trip), run against in-memory
SQLite (see tests/conftest.py)."""

from datetime import date
from uuid import uuid4

from sqlalchemy import select

from app.models import MitigationInput
from app.repositories.penalties.mitigation import MitigationInputRepository
from app.services.penalties.mitigation import MitigationEngine, MitigationInputs, ShortageCause
from app.services.penalties.projection import (
    SHORTAGE_VIOLATION_TYPES,
    CalcType,
    OrderSnapshot,
    PenaltyRule,
    ProductionStatus,
    ProjectionEngine,
)
from app.services.seeding.scenario_data_mitigation import (
    EXPENSIVE_CARRIER_INPUTS,
    EXPENSIVE_CARRIER_RULES,
    EXPENSIVE_CARRIER_SNAPSHOT,
    MIXED_SHORTAGE_DELAY_INPUTS,
    MIXED_SHORTAGE_DELAY_RULES,
    MIXED_SHORTAGE_DELAY_SNAPSHOT,
    SEEDED_MITIGATION_INPUTS,
)
from app.services.seeding.scenario_data_projection import AMZ_RULES, WMT_RULES


def _options_by_action(options):
    return {o.action: o for o in options}


def test_not_present_tier_only_accept_is_eligible():
    """AMZ-778501's real assignment: no MitigationInputs row at all --
    caller passes the all-default MitigationInputs, same as what
    MitigationRepository.get_inputs returns for an absent order. Real
    shortfall exists (AT_RISK, anticipated) but confirmed_qty == order_qty
    keeps SPLIT_SHIPMENT ineligible too, and no cost data means
    SPEED_UP_PRODUCTION/FASTER_CARRIER are hard-excluded -- ACCEPT alone."""
    snapshot = OrderSnapshot(
        order_id="AMZ-778501",
        projection_date=date(2026, 8, 4),
        order_qty=1200,
        unit_price=14.0,
        requested_delivery_date=date(2026, 8, 14),
        required_ship_date=date(2026, 8, 12),
        confirmed_qty=1200,
        production_status=ProductionStatus.AT_RISK,
    )
    projection = ProjectionEngine().project(snapshot, AMZ_RULES)
    inputs = MitigationInputs(order_id="AMZ-778501")  # the "not present" default

    options = MitigationEngine().evaluate(snapshot, AMZ_RULES, projection, inputs)

    assert {o.action for o in options} == {"ACCEPT"}
    assert options[0].net_saving == 0.0


def test_partial_estimated_tier_gives_estimated_medium_risk_options():
    """WMT-100511: cause/costs known, none confirmed -- both
    SPEED_UP_PRODUCTION and FASTER_CARRIER should surface as ESTIMATED/MEDIUM."""
    snapshot = OrderSnapshot(
        order_id="WMT-100511",
        projection_date=date(2026, 8, 11),
        order_qty=1500,
        unit_price=18.0,
        requested_delivery_date=date(2026, 8, 15),
        required_ship_date=date(2026, 8, 13),
        confirmed_qty=1440,  # confirmed 60-unit shortfall
        production_status=ProductionStatus.AT_RISK,
    )
    projection = ProjectionEngine().project(snapshot, WMT_RULES)
    inputs = SEEDED_MITIGATION_INPUTS["WMT-100511"]

    options = MitigationEngine().evaluate(snapshot, WMT_RULES, projection, inputs)
    by_action = _options_by_action(options)

    assert by_action["SPEED_UP_PRODUCTION"].confidence == "ESTIMATED"
    assert by_action["SPEED_UP_PRODUCTION"].risk_level == "MEDIUM"
    assert by_action["FASTER_CARRIER"].confidence == "ESTIMATED"
    assert by_action["FASTER_CARRIER"].risk_level == "MEDIUM"
    assert "ACCEPT" in by_action


def test_full_confirmed_tier_gives_confirmed_low_risk_options():
    """WMT-100234: every field known and confirmed, comfortable time
    margin -- SPEED_UP_PRODUCTION and FASTER_CARRIER both CONFIRMED/LOW."""
    snapshot = OrderSnapshot(
        order_id="WMT-100234",
        projection_date=date(2026, 8, 4),
        order_qty=2000,
        unit_price=18.0,
        requested_delivery_date=date(2026, 8, 11),
        required_ship_date=date(2026, 8, 9),
        confirmed_qty=1900,  # confirmed 100-unit shortfall, 5 days available
        production_status=ProductionStatus.AT_RISK,
    )
    projection = ProjectionEngine().project(snapshot, WMT_RULES)
    inputs = SEEDED_MITIGATION_INPUTS["WMT-100234"]

    options = MitigationEngine().evaluate(snapshot, WMT_RULES, projection, inputs)
    by_action = _options_by_action(options)

    speed_up = by_action["SPEED_UP_PRODUCTION"]
    assert speed_up.confidence == "CONFIRMED"
    assert speed_up.risk_level == "LOW"
    # Hand-verifiable: closes the full 100-unit shortfall (250/day x 5
    # days available >> 100) at $3.50/unit.
    assert speed_up.action_cost == 350.00

    faster_carrier = by_action["FASTER_CARRIER"]
    assert faster_carrier.confidence == "CONFIRMED"
    assert faster_carrier.action_cost == 950.00


def test_hard_exclude_tier_excludes_speed_up_production_on_cause_alone():
    """AMZ-780112: cost data is present (cheaper than WMT-100234's, even),
    but the cause is a *confirmed* RAW_MATERIAL shortage -- proves the
    exclusion is cause-based, not data-absence-based."""
    snapshot = OrderSnapshot(
        order_id="AMZ-780112",
        projection_date=date(2026, 8, 12),
        order_qty=900,
        unit_price=14.0,
        requested_delivery_date=date(2026, 8, 18),
        required_ship_date=date(2026, 8, 16),
        confirmed_qty=820,  # confirmed 80-unit shortfall
        production_status=ProductionStatus.AT_RISK,
    )
    projection = ProjectionEngine().project(snapshot, AMZ_RULES)
    inputs = SEEDED_MITIGATION_INPUTS["AMZ-780112"]
    assert inputs.shortage_cause == ShortageCause.RAW_MATERIAL
    assert inputs.capacity_boost_cost_per_unit is not None  # data present anyway

    options = MitigationEngine().evaluate(snapshot, AMZ_RULES, projection, inputs)

    assert "SPEED_UP_PRODUCTION" not in {o.action for o in options}
    assert "FASTER_CARRIER" in {o.action for o in options}  # unaffected by the shortage cause
    assert "SPLIT_SHIPMENT" in {o.action for o in options}  # confirmed_qty < order_qty


def test_accept_always_present_and_never_dropped():
    for snapshot, rules, inputs in [
        (
            EXPENSIVE_CARRIER_SNAPSHOT,
            EXPENSIVE_CARRIER_RULES,
            EXPENSIVE_CARRIER_INPUTS,
        ),
        (
            MIXED_SHORTAGE_DELAY_SNAPSHOT,
            MIXED_SHORTAGE_DELAY_RULES,
            MIXED_SHORTAGE_DELAY_INPUTS,
        ),
    ]:
        projection = ProjectionEngine().project(snapshot, rules)
        options = MitigationEngine().evaluate(snapshot, rules, projection, inputs)
        assert any(o.action == "ACCEPT" for o in options)


def test_options_sorted_by_net_saving_descending():
    projection = ProjectionEngine().project(MIXED_SHORTAGE_DELAY_SNAPSHOT, MIXED_SHORTAGE_DELAY_RULES)
    options = MitigationEngine().evaluate(
        MIXED_SHORTAGE_DELAY_SNAPSHOT, MIXED_SHORTAGE_DELAY_RULES, projection, MIXED_SHORTAGE_DELAY_INPUTS
    )

    savings = [o.net_saving for o in options]
    assert savings == sorted(savings, reverse=True)


def test_paid_option_that_costs_more_than_it_saves_still_loses_to_accept():
    """FASTER_CARRIER is structurally eligible (fully confirmed data) but
    its $999 cost dwarfs the $200 flat fee it would avoid -- ACCEPT must
    still rank first, and FASTER_CARRIER must still be present (a bad
    deal, not an ineligible one)."""
    projection = ProjectionEngine().project(EXPENSIVE_CARRIER_SNAPSHOT, EXPENSIVE_CARRIER_RULES)
    options = MitigationEngine().evaluate(
        EXPENSIVE_CARRIER_SNAPSHOT, EXPENSIVE_CARRIER_RULES, projection, EXPENSIVE_CARRIER_INPUTS
    )
    by_action = _options_by_action(options)

    assert options[0].action == "ACCEPT"
    assert "FASTER_CARRIER" in by_action
    assert by_action["FASTER_CARRIER"].net_saving < 0
    assert by_action["FASTER_CARRIER"].confidence == "CONFIRMED"


def test_split_shipment_zeroes_only_the_delay_component():
    """Mixed shortage+delay order: SPLIT_SHIPMENT's projected_penalty_after
    must equal only the shortage-side violation, proving the delay
    component (OTIF_LATE) was zeroed and the shortage component wasn't."""
    projection = ProjectionEngine().project(MIXED_SHORTAGE_DELAY_SNAPSHOT, MIXED_SHORTAGE_DELAY_RULES)
    options = MitigationEngine().evaluate(
        MIXED_SHORTAGE_DELAY_SNAPSHOT, MIXED_SHORTAGE_DELAY_RULES, projection, MIXED_SHORTAGE_DELAY_INPUTS
    )
    split = _options_by_action(options)["SPLIT_SHIPMENT"]

    shortage_only_penalty = sum(
        v.expected_penalty_amount
        for v in projection.violations
        if v.violation_type in SHORTAGE_VIOLATION_TYPES
    )
    assert split.projected_penalty_after == round(shortage_only_penalty, 2)
    assert split.projected_penalty_after < projection.total_expected_penalty_amount
    assert split.action_cost == MIXED_SHORTAGE_DELAY_INPUTS.split_shipment_handling_cost


def test_faster_carrier_hard_excluded_when_no_delay_type_rule_applies():
    snapshot = OrderSnapshot(
        order_id="MIT-NO-DELAY-RULE",
        projection_date=date(2026, 8, 1),
        order_qty=1000,
        unit_price=10.0,
        requested_delivery_date=date(2026, 8, 6),
        required_ship_date=date(2026, 8, 4),
        confirmed_qty=1000,
        production_status=ProductionStatus.ON_TRACK,
    )
    rules = [PenaltyRule("RULE-NO-DELAY", "SHORT_SHIP", CalcType.PER_UNIT, rate=3.0, threshold_pct=0.0)]
    inputs = MitigationInputs(
        order_id="MIT-NO-DELAY-RULE",
        express_carrier_cost=100.0,
        express_carrier_transit_days=1,
        express_carrier_data_confirmed=True,
    )
    projection = ProjectionEngine().project(snapshot, rules)

    options = MitigationEngine().evaluate(snapshot, rules, projection, inputs)

    assert "FASTER_CARRIER" not in {o.action for o in options}


# ---------------------------------------------------------------------
# Repository-level (DB) tests -- was against the old MitigationRepository
# (business-string order_id); now MitigationInputRepository, keyed by the
# UUID surrogate purchase_order_id (see app/repositories/penalties/mitigation.py).
# ---------------------------------------------------------------------


def test_get_inputs_returns_defaults_when_absent(db_session):
    repo = MitigationInputRepository(db_session)

    missing_id = uuid4()
    inputs = repo.get_inputs(missing_id)

    assert inputs == MitigationInputs(order_id=str(missing_id))


def test_feasibility_cutoff_carrier_infeasible():
    """Cutoff: When remaining days to MABD delivery is strictly less than
    express transit days (e.g. 1 day left to delivery but express transit requires 2 days),
    FASTER_CARRIER is physically impossible and must be excluded."""
    snapshot = OrderSnapshot(
        order_id="WMT-100234",
        projection_date=date(2026, 8, 10),
        requested_delivery_date=date(2026, 8, 11),  # exactly 1 day to delivery
        required_ship_date=date(2026, 8, 9),
        order_qty=2000,
        unit_price=18.0,
        confirmed_qty=1900,
        production_status=ProductionStatus.AT_RISK,
    )
    projection = ProjectionEngine().project(snapshot, WMT_RULES)
    inputs = MitigationInputs(
        order_id="WMT-100234",
        express_carrier_cost=85.0,
        express_carrier_transit_days=2,  # 2 days transit required, but only 1 day to delivery
        express_carrier_data_confirmed=True,
    )
    options = MitigationEngine().evaluate(snapshot, WMT_RULES, projection, inputs)
    actions = {o.action for o in options}
    assert "FASTER_CARRIER" not in actions, (
        "Carrier expedite must be excluded when transit exceeds days to delivery"
    )


def test_upsert_then_get_round_trips(repos, db_session):
    retailer = repos.master_data.add_retailer("RET-MIT", "Mitigation Test Co", None)
    material = repos.master_data.add_material("MAT-MIT", None)
    plant = repos.master_data.add_plant("PLANT-MIT", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="ORD-MIT",
        retailer_id=retailer["id"],
        order_date=date(2026, 8, 1),
        requested_delivery_date=date(2026, 8, 10),
        required_ship_date=date(2026, 8, 8),
    )
    repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=5.0,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    purchase_order_id = purchase_order["id"]

    repos.mitigation_inputs.upsert_inputs(
        purchase_order_id=purchase_order_id,
        shortage_cause="LABOR_CAPACITY",
        shortage_cause_confirmed=True,
        capacity_boost_cost_per_unit=2.50,
        capacity_boost_max_units_per_day=50.0,
        capacity_boost_data_confirmed=True,
        express_carrier_cost=200.0,
        express_carrier_transit_days=1,
        express_carrier_data_confirmed=True,
        split_shipment_handling_cost=25.0,
    )

    inserted = repos.mitigation_inputs.get_inputs(purchase_order_id)
    assert inserted.shortage_cause == ShortageCause.LABOR_CAPACITY
    assert inserted.capacity_boost_cost_per_unit == 2.50

    # Idempotent: calling again with a changed field updates in place,
    # rather than raising a duplicate-key error on purchase_order_id.
    repos.mitigation_inputs.upsert_inputs(
        purchase_order_id=purchase_order_id, capacity_boost_cost_per_unit=9.99
    )
    updated = repos.mitigation_inputs.get_inputs(purchase_order_id)
    assert updated.capacity_boost_cost_per_unit == 9.99
    assert updated.shortage_cause == ShortageCause.LABOR_CAPACITY  # untouched fields survive

    rows = db_session.scalars(
        select(MitigationInput).where(MitigationInput.purchase_order_id == purchase_order_id)
    ).all()
    assert len(rows) == 1  # updated in place, not duplicated
