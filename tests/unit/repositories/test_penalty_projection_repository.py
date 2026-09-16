"""Tests for `PenaltyProjectionRepository.save_result`'s return value: a
`rule_id -> persisted penalty_projection.id` mapping, added so
`ProjectionService.run_for_purchase_order` (and, through it,
`POST /penalties/projections`) can hand a client the id of the row it just
persisted for each violation without a separate read -- see
`app.services.penalties.projection.types.ViolationProjection.id`'s
docstring for why this exists."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from app.services.penalties.projection import ProjectionResult, ViolationProjection
from tests.conftest import make_retailer_agreement


def _seed_rule_order(repos, po_number: str) -> tuple[UUID, UUID]:
    retailer = repos.master_data.add_retailer(f"RET-{po_number}", "Retailer", None, "SUM")
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=po_number,
        retailer_id=retailer["id"],
        order_date=date(2026, 8, 1),
        requested_delivery_date=date(2026, 8, 10),
        required_ship_date=date(2026, 8, 8),
    )
    rule = repos.penalty_rules.add_rule(
        rule_code=f"RULE-{po_number}",
        retailer_id=retailer["id"],
        violation_type="OTIF_LATE",
        penalty_category="OTIF_LATE",
        retailer_agreement_id=make_retailer_agreement(repos, retailer["id"]),
        calc_type="FLAT_FEE",
        rate=50.0,
    )
    return purchase_order["id"], rule["id"]


def _result(rule_id: UUID, projection_date: date) -> ProjectionResult:
    return ProjectionResult(
        order_id="unused",
        projection_date=projection_date,
        days_to_delivery=3,
        shortage_probability=0.0,
        delay_probability=0.05,
        violations=[
            ViolationProjection(
                violation_type="OTIF_LATE",
                rule_id=str(rule_id),
                probability=0.05,
                penalty_amount=50.0,
                expected_penalty_amount=2.5,
            )
        ],
        total_expected_penalty_amount=2.5,
        stacking_mode="SUM",
    )


def test_save_result_returns_the_persisted_id_for_a_new_row(repos):
    purchase_order_id, rule_id = _seed_rule_order(repos, "ORD-PID-1")

    ids_by_rule_id = repos.penalty_projections.save_result(
        purchase_order_id, _result(rule_id, date(2026, 8, 5))
    )

    assert set(ids_by_rule_id) == {str(rule_id)}
    persisted_id = ids_by_rule_id[str(rule_id)]
    assert isinstance(persisted_id, UUID)

    # Not invented/derived -- it's the id of the row `get_by_id` (and every
    # GET route) reads back.
    row = repos.penalty_projections.get_by_id(persisted_id)
    assert row is not None
    assert row["purchase_order_id"] == purchase_order_id
    assert row["rule_id"] == rule_id


def test_save_result_returns_the_same_id_across_an_upsert_of_the_same_row(repos):
    """Re-running a projection for the same (purchase_order_id, rule_id,
    projection_date) updates the existing row in place (see `save_result`'s
    `existing is not None` branch) -- its id must not change underneath a
    client that cached it from the first run."""
    purchase_order_id, rule_id = _seed_rule_order(repos, "ORD-PID-2")
    projection_date = date(2026, 8, 5)

    first_ids = repos.penalty_projections.save_result(purchase_order_id, _result(rule_id, projection_date))
    second_ids = repos.penalty_projections.save_result(purchase_order_id, _result(rule_id, projection_date))

    assert first_ids[str(rule_id)] == second_ids[str(rule_id)]
