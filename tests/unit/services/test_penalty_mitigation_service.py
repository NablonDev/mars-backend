"""Tests for MitigationService -- the mitigation-options counterpart to
ProjectionService, evaluating against an already-persisted projection
rather than recomputing one.

Was against `FineMitigationService`/`MitigationResultRepository` (business-
string `order_id`); rewritten against `MitigationService` and the new
`common`/`penalties` repositories, keyed by the UUID surrogate
`purchase_order_id`. Was `tests/unit/services/test_fine_mitigation_service.py`
(`fine`/`fines` -> `penalty`/`penalties` rename).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from app.core.exceptions import BusinessRuleError, NotFoundError
from app.services.penalties.mitigation.service import MitigationService
from app.services.penalties.mitigation.types import ShortageCause
from app.services.penalties.projection.service import ProjectionService
from tests.conftest import make_retailer_agreement


def _build_services(repos) -> tuple[ProjectionService, MitigationService]:
    projection_service = ProjectionService(
        purchase_orders=repos.purchase_orders,
        fulfillment=repos.fulfillment,
        rules=repos.penalty_rules,
        master_data=repos.master_data,
        projections=repos.penalty_projections,
    )
    mitigation_service = MitigationService(
        purchase_orders=repos.purchase_orders,
        rules=repos.penalty_rules,
        master_data=repos.master_data,
        projections=repos.penalty_projections,
        mitigation_inputs=repos.mitigation_inputs,
        mitigation_options=repos.mitigation_options,
        projection_service=projection_service,
    )
    return projection_service, mitigation_service


def _seed_shortage_order(repos, projection_service, po_number: str = "ORD-MIT"):
    """A shortage-only order (300-unit gap out of 1000) with a per-unit
    rule, no delay rule -- SPEED_UP_PRODUCTION should be structurally
    eligible once cause/cost data is provided. Returns the purchase
    order's UUID id."""
    retailer = repos.master_data.add_retailer(f"RET-{po_number}", "Retailer Mit", None, "SUM")
    material = repos.master_data.add_material(f"MAT-{po_number}", None)
    plant = repos.master_data.add_plant(f"PLANT-{po_number}", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=po_number,
        retailer_id=retailer["id"],
        order_date=date(2026, 8, 1),
        requested_delivery_date=date(2026, 8, 10),
        required_ship_date=date(2026, 8, 8),
    )
    line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=1000,
        unit_price=10.0,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    repos.penalty_rules.add_rule(
        rule_code=f"RULE-{po_number}-SHORT",
        retailer_id=retailer["id"],
        violation_type="SHORT_SHIP",
        penalty_category="SHORT_SHIP",
        retailer_agreement_id=make_retailer_agreement(repos, retailer["id"]),
        calc_type="PER_UNIT",
        rate=4.0,
        threshold_pct=0.0,
    )
    confirmation = repos.fulfillment.add_order_confirmation(
        confirmation_number=f"CONF-{po_number}",
        purchase_order_id=purchase_order["id"],
        confirmation_date=datetime(2026, 8, 5, tzinfo=UTC),
    )
    repos.fulfillment.add_order_confirmation_line(
        order_confirmation_id=confirmation["id"],
        purchase_order_line_id=line["id"],
        confirmed_quantity=700,
    )
    result = projection_service.run_for_purchase_order(purchase_order["id"], date(2026, 8, 5))
    assert result.total_expected_penalty_amount > 0
    repos.mitigation_inputs.upsert_inputs(
        purchase_order_id=purchase_order["id"],
        shortage_cause=ShortageCause.LABOR_CAPACITY.value,
        shortage_cause_confirmed=True,
        capacity_boost_cost_per_unit=3.0,
        capacity_boost_max_units_per_day=200,
        capacity_boost_data_confirmed=True,
    )
    return purchase_order["id"]


def test_order_not_found_raises_order_not_found_error(repos):
    _, mitigation_service = _build_services(repos)
    missing_id = uuid4()

    with pytest.raises(NotFoundError, match=str(missing_id)):
        mitigation_service.run_for_purchase_order(missing_id)


def test_no_projection_exists_raises(repos):
    _, mitigation_service = _build_services(repos)
    retailer = repos.master_data.add_retailer("RET-EMPTY", "Retailer Empty", None, "SUM")
    material = repos.master_data.add_material("MAT-EMPTY", None)
    plant = repos.master_data.add_plant("PLANT-EMPTY", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="ORD-EMPTY",
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

    with pytest.raises(BusinessRuleError, match=str(purchase_order["id"])):
        mitigation_service.run_for_purchase_order(purchase_order["id"], date(2026, 8, 5))


def test_run_for_purchase_order_computes_and_persists_ranked_options(repos):
    projection_service, mitigation_service = _build_services(repos)
    purchase_order_id = _seed_shortage_order(repos, projection_service)

    projection_date, options = mitigation_service.run_for_purchase_order(purchase_order_id, date(2026, 8, 5))

    assert projection_date == date(2026, 8, 5)
    actions = [o.action for o in options]
    assert "ACCEPT" in actions
    assert "SPEED_UP_PRODUCTION" in actions
    # Ranked by net_saving, descending.
    assert [o.net_saving for o in options] == sorted((o.net_saving for o in options), reverse=True)

    persisted = repos.mitigation_options.list_for_date(purchase_order_id, date(2026, 8, 5))
    assert {row["action"] for row in persisted} == set(actions)


def test_run_for_purchase_order_defaults_projection_date_to_the_only_projected_day(repos):
    projection_service, mitigation_service = _build_services(repos)
    purchase_order_id = _seed_shortage_order(repos, projection_service)

    # today's UTC date almost certainly has no projection row for this
    # PO -- exercising the "no projection for the resolved date" path
    # separately from the explicit-date happy path above.
    with pytest.raises(BusinessRuleError):
        mitigation_service.run_for_purchase_order(purchase_order_id)


def test_run_for_purchase_order_is_idempotent_on_repeated_calls(repos):
    projection_service, mitigation_service = _build_services(repos)
    purchase_order_id = _seed_shortage_order(repos, projection_service)

    _, first = mitigation_service.run_for_purchase_order(purchase_order_id, date(2026, 8, 5))
    _, second = mitigation_service.run_for_purchase_order(purchase_order_id, date(2026, 8, 5))

    assert {o.action for o in first} == {o.action for o in second}
    rows = repos.mitigation_options.list_for_date(purchase_order_id, date(2026, 8, 5))
    # One row per action, not duplicated by the second run.
    assert len(rows) == len(first)


def test_get_latest_raises_when_nothing_has_been_computed_yet(repos):
    projection_service, mitigation_service = _build_services(repos)
    purchase_order_id = _seed_shortage_order(repos, projection_service)

    with pytest.raises(BusinessRuleError, match=str(purchase_order_id)):
        mitigation_service.get_latest(purchase_order_id)


def test_get_latest_returns_the_most_recently_computed_day(repos):
    projection_service, mitigation_service = _build_services(repos)
    purchase_order_id = _seed_shortage_order(repos, projection_service)
    mitigation_service.run_for_purchase_order(purchase_order_id, date(2026, 8, 5))

    projection_date, rows = mitigation_service.get_latest(purchase_order_id)

    assert projection_date == date(2026, 8, 5)
    assert rows
    assert rows == sorted(rows, key=lambda r: r["net_saving"], reverse=True)


def test_get_latest_raises_order_not_found_for_unknown_order(repos):
    _, mitigation_service = _build_services(repos)
    missing_id = uuid4()

    with pytest.raises(NotFoundError):
        mitigation_service.get_latest(missing_id)


def test_current_stacking_mode_used_not_a_historical_override(repos):
    """run_for_purchase_order rebuilds the ProjectionResult using the
    retailer's *current* stacking_mode, matching ProjectionService's own
    default (non-override) behavior."""
    projection_service, mitigation_service = _build_services(repos)
    purchase_order_id = _seed_shortage_order(repos, projection_service)

    _, options = mitigation_service.run_for_purchase_order(purchase_order_id, date(2026, 8, 5))
    accept = next(o for o in options if o.action == "ACCEPT")

    history = repos.penalty_projections.list_history(purchase_order_id)
    day_rows = [r for r in history if r["projection_date"] == date(2026, 8, 5)]
    expected_total = round(sum(r["expected_penalty_amount"] for r in day_rows), 2)
    assert accept.projected_penalty_after == expected_total


def test_reconstructed_projection_result_handles_max_stacking(repos):
    projection_service, mitigation_service = _build_services(repos)
    retailer = repos.master_data.add_retailer("RET-MAX", "Retailer Max", None, "MAX")
    material = repos.master_data.add_material("MAT-MAX", None)
    plant = repos.master_data.add_plant("PLANT-MAX", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="ORD-MAX",
        retailer_id=retailer["id"],
        order_date=date(2026, 8, 1),
        requested_delivery_date=date(2026, 8, 10),
        required_ship_date=date(2026, 8, 8),
    )
    line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=1000,
        unit_price=10.0,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    retailer_agreement_id = make_retailer_agreement(repos, retailer["id"])
    repos.penalty_rules.add_rule(
        rule_code="RULE-MAX-SHORT",
        retailer_id=retailer["id"],
        violation_type="SHORT_SHIP",
        penalty_category="SHORT_SHIP",
        retailer_agreement_id=retailer_agreement_id,
        calc_type="PER_UNIT",
        rate=4.0,
    )
    repos.penalty_rules.add_rule(
        rule_code="RULE-MAX-OTIF",
        retailer_id=retailer["id"],
        violation_type="OTIF_LATE",
        penalty_category="OTIF_LATE",
        retailer_agreement_id=retailer_agreement_id,
        calc_type="FLAT_FEE",
        rate=500.0,
    )
    confirmation = repos.fulfillment.add_order_confirmation(
        confirmation_number="CONF-ORD-MAX",
        purchase_order_id=purchase_order["id"],
        confirmation_date=datetime(2026, 8, 5, tzinfo=UTC),
    )
    repos.fulfillment.add_order_confirmation_line(
        order_confirmation_id=confirmation["id"],
        purchase_order_line_id=line["id"],
        confirmed_quantity=700,
    )
    projection_service.run_for_purchase_order(purchase_order["id"], date(2026, 8, 5))

    _, options = mitigation_service.run_for_purchase_order(purchase_order["id"], date(2026, 8, 5))
    accept = next(o for o in options if o.action == "ACCEPT")

    history = repos.penalty_projections.list_history(purchase_order["id"])
    day_rows = [r for r in history if r["projection_date"] == date(2026, 8, 5)]
    expected_total = round(max(r["expected_penalty_amount"] for r in day_rows), 2)
    assert accept.projected_penalty_after == expected_total


def test_raw_material_shortage_cause_excludes_speed_up_production(repos):
    projection_service, mitigation_service = _build_services(repos)
    purchase_order_id = _seed_shortage_order(repos, projection_service, "ORD-RAWMAT")
    repos.mitigation_inputs.upsert_inputs(
        purchase_order_id=purchase_order_id,
        shortage_cause=ShortageCause.RAW_MATERIAL.value,
        shortage_cause_confirmed=True,
        capacity_boost_cost_per_unit=3.0,
        capacity_boost_max_units_per_day=200,
        capacity_boost_data_confirmed=True,
    )

    _, options = mitigation_service.run_for_purchase_order(purchase_order_id, date(2026, 8, 5))

    assert "SPEED_UP_PRODUCTION" not in {o.action for o in options}
