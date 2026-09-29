"""Repository-layer tests, run against in-memory SQLite (see conftest.py).
Was tests/unit/repositories/test_repositories.py against the old
FineRuleRepository/OrderRepository -- relocated onto PenaltyRuleRepository
(app.repositories.penalties.rule) and the split
common/{master_data,purchase_order,fulfillment} repositories.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.core.exceptions import ValidationError
from app.models import DemandException, OrderConfirmation, ProductionSchedule, Retailer, Shipment
from app.models.penalties import PenaltyRuleTier
from app.services.penalties.projection import CalcType
from tests.conftest import make_retailer_agreement


def test_tiered_rule_loads_its_bands(repos, db_session):
    db_session.add(Retailer(retailer_code="RET-X", retailer_name="Retailer X"))
    db_session.flush()
    retailer = db_session.scalars(select(Retailer).where(Retailer.retailer_code == "RET-X")).first()

    rule = repos.penalty_rules.add_rule(
        rule_code="RULE-TIERED",
        violation_type="FILL_RATE",
        penalty_category="SHORT_SHIP",
        retailer_agreement_id=make_retailer_agreement(repos, retailer.id),
        calc_type="TIERED",
        rate=0.0,
        threshold_pct=0.0,
        tiers=[
            {"band_min": 0.0, "band_max": 0.10, "rate": 0.02},
            {"band_min": 0.10, "band_max": 0.30, "rate": 0.05},
        ],
    )
    assert rule["rule_code"] == "RULE-TIERED"

    rules = repos.penalty_rules.list_rules_for_retailer(retailer.id)

    assert len(rules) == 1
    assert rules[0].calc_type == CalcType.TIERED
    assert rules[0].tiers is not None
    assert len(rules[0].tiers) == 2
    assert rules[0].tiers[0].band_min == 0.0
    assert rules[0].tiers[1].rate == 0.05


def test_unrecognized_calc_type_raises_invalid_penalty_rule_data_error(repos, db_session):
    db_session.add(Retailer(retailer_code="RET-Y", retailer_name="Retailer Y"))
    db_session.flush()
    retailer = db_session.scalars(select(Retailer).where(Retailer.retailer_code == "RET-Y")).first()

    repos.penalty_rules.add_rule(
        rule_code="RULE-BAD",
        violation_type="SHORT_SHIP",
        penalty_category="SHORT_SHIP",
        retailer_agreement_id=make_retailer_agreement(repos, retailer.id),
        calc_type="NOT_A_REAL_CALC_TYPE",
        rate=1.0,
    )

    with pytest.raises(ValidationError, match="RULE-BAD"):
        repos.penalty_rules.list_rules_for_retailer(retailer.id)


def test_add_rule_via_repository_persists_tiers(repos):
    retailer = repos.master_data.add_retailer("RET-Z", "Retailer Z", None, "SUM")
    repos.penalty_rules.add_rule(
        rule_code="RULE-Z",
        violation_type="FILL_RATE",
        penalty_category="SHORT_SHIP",
        retailer_agreement_id=make_retailer_agreement(repos, retailer["id"]),
        calc_type="TIERED",
        rate=0.0,
        threshold_pct=0.0,
        cap_amount=None,
        tiers=[
            {"band_min": 0.0, "band_max": 0.10, "rate": 0.02},
            {"band_min": 0.10, "band_max": 1.01, "rate": 0.08},
        ],
    )

    rules = repos.penalty_rules.list_rules_for_retailer(retailer["id"])
    assert len(rules) == 1
    assert len(rules[0].tiers) == 2
    assert rules[0].tiers[1].rate == 0.08


def test_tier_code_is_generated_from_position(repos, db_session):
    """uq_penalty_rule_tier_rule_code -- DB-enforced idempotency guard,
    unlike production_schedule (see fulfillment repository docstring)."""
    retailer = repos.master_data.add_retailer("RET-TIER", "Retailer Tier", None, "SUM")
    repos.penalty_rules.add_rule(
        rule_code="RULE-TIER-DUP",
        violation_type="FILL_RATE",
        penalty_category="SHORT_SHIP",
        retailer_agreement_id=make_retailer_agreement(repos, retailer["id"]),
        calc_type="TIERED",
        rate=0.0,
        tiers=[{"band_min": 0.0, "band_max": 0.10, "rate": 0.02}],
    )
    tier = db_session.scalars(select(PenaltyRuleTier)).first()
    assert tier.tier_code == "TIER-00"


def _seed_purchase_order(repos, number: str) -> dict:
    retailer = repos.master_data.add_retailer(f"RET-{number}", "Retailer", None, "SUM")
    material = repos.master_data.add_material(f"MAT-{number}", None)
    plant = repos.master_data.add_plant(f"PLANT-{number}", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=number,
        retailer_id=retailer["id"],
        order_date=date(2026, 8, 1),
        requested_delivery_date=date(2026, 8, 10),
        required_ship_date=date(2026, 8, 8),
    )
    line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=25.00,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    return {"purchase_order": purchase_order, "line": line, "material": material, "plant": plant}


def test_fact_writers_are_idempotent_on_their_natural_key(repos, db_session):
    """Regression test for a real bug: calling each of these twice with
    the same natural-key id used to raise IntegrityError (duplicate key)
    against a real, persistent database -- exactly what a seeding replay
    does if it's ever called more than once (e.g. resetting a demo),
    since its ids are deterministic ("CONF-{po}-{date}", etc.), not
    something the in-memory, fresh-per-test SQLite database in every
    other test could ever expose."""
    seeded = _seed_purchase_order(repos, "ORD-IDEM")
    purchase_order_id = seeded["purchase_order"]["id"]
    line_id = seeded["line"]["id"]
    material_id = seeded["material"]["id"]
    plant_id = seeded["plant"]["id"]

    for _ in range(2):
        confirmation = repos.fulfillment.add_order_confirmation(
            confirmation_number="CONF-ORD-IDEM-01",
            purchase_order_id=purchase_order_id,
            confirmation_date=datetime(2026, 8, 2, tzinfo=UTC),
        )
        repos.fulfillment.add_order_confirmation_line(
            order_confirmation_id=confirmation["id"],
            purchase_order_line_id=line_id,
            confirmed_quantity=90,
        )
        repos.fulfillment.add_production_schedule(
            material_id=material_id,
            plant_id=plant_id,
            status="AT_RISK",
            status_at=datetime(2026, 8, 2, tzinfo=UTC),
        )
        delivery = repos.fulfillment.add_delivery(
            delivery_number="DELIV-ORD-IDEM-01",
            purchase_order_id=purchase_order_id,
        )
        repos.fulfillment.add_shipment(
            shipment_number="SHIP-ORD-IDEM-20260802000000",
            delivery_id=delivery["id"],
            recorded_at=datetime(2026, 8, 2, tzinfo=UTC),
        )
        repos.fulfillment.add_demand_exception(
            exception_id="EXC-ORD-IDEM-01", purchase_order_line_id=line_id, flagged_date=date(2026, 8, 2)
        )

    confirmations = db_session.scalars(
        select(OrderConfirmation).where(OrderConfirmation.confirmation_number == "CONF-ORD-IDEM-01")
    ).all()
    productions = db_session.scalars(
        select(ProductionSchedule).where(
            ProductionSchedule.material_id == material_id,
            ProductionSchedule.plant_id == plant_id,
        )
    ).all()
    shipments = db_session.scalars(
        select(Shipment).where(Shipment.shipment_number == "SHIP-ORD-IDEM-20260802000000")
    ).all()
    exceptions = db_session.scalars(
        select(DemandException).where(DemandException.exception_id == "EXC-ORD-IDEM-01")
    ).all()

    assert len(confirmations) == 1
    assert len(productions) == 1
    assert len(shipments) == 1
    assert len(exceptions) == 1


def test_add_actual_penalty_persists_purchase_order_line_id(repos):
    """Verify that `add_actual_penalty` preserves a given `purchase_order_line_id`."""
    seeded = _seed_purchase_order(repos, "ORD-ACTUAL-PENALTY-LINE-ID")
    purchase_order_id = seeded["purchase_order"]["id"]
    purchase_order_line_id = seeded["line"]["id"]

    result = repos.actual_penalties.add_actual_penalty(
        actual_penalty_number="AP-TEST-001",
        purchase_order_id=purchase_order_id,
        violation_type="OTIF_LATE",
        actual_penalty_amount=100.0,
        invoice_or_deduction_date=date(2026, 1, 1),
        purchase_order_line_id=purchase_order_line_id,
    )
    assert result["purchase_order_line_id"] == purchase_order_line_id
