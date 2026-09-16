"""Tests for `ProjectionService.run_commitment_projection` (Phase 3c) and the pure
`projection/commitment.py` helpers it's built on.

Zero-LLM, DB-backed via the SQLite test session (see `tests/conftest.py`).
"""

from datetime import date

import pytest

from app.services.penalties.projection.commitment import (
    compute_commitment_shortfall_probability,
    project_full_window_total,
)
from app.services.penalties.projection.service import ProjectionService
from tests.conftest import make_retailer_agreement


def _build_service(repos) -> ProjectionService:
    return ProjectionService(
        purchase_orders=repos.purchase_orders,
        fulfillment=repos.fulfillment,
        rules=repos.penalty_rules,
        master_data=repos.master_data,
        projections=repos.penalty_projections,
        retailer_agreements=repos.retailer_agreements,
    )


def test_50000_committed_38000_ordered_projects_12000_shortfall_no_per_po_violation(repos):
    """Acceptance scenario from the integration plan's section 9: a retailer committed to
    50,000 cases with 38,000 ordered inside the measurement window projects a 12,000-case
    shortfall, and that retailer's own per-PO projection carries no volume-commitment
    violation (the rule is SHORTAGE/DELAY-shaped-only territory; VOLUME_COMMITMENT is
    contract-period grain and lands in ProjectionResult.skipped instead)."""
    retailer = repos.master_data.add_retailer("RET-VC", "Volume Commitment Co", None, "SUM")
    material = repos.master_data.add_material("MAT-VC", None)
    plant = repos.master_data.add_plant("PLANT-VC", None, None)
    agreement_id = make_retailer_agreement(repos, retailer["id"])

    # ROLLING (the default) trails as_of_date, so window_end == as_of_date and the
    # run-rate model's elapsed/total fraction is exactly 1: the projected full-window
    # total is the actual-to-date total, unextrapolated, keeping this scenario's numbers
    # hand-verifiable.
    as_of_date = date(2026, 12, 31)
    repos.penalty_rules.add_rule(
        rule_code="RULE-VC-COMMIT",
        retailer_id=retailer["id"],
        violation_type="VOLUME_SHORTFALL",
        penalty_category="MINIMUM_VOLUME_SHORTFALL",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=2.0,
        engine_family="VOLUME_COMMITMENT",
        commitment_quantity=50000,
        effective_start_date=date(2026, 1, 1),
    )

    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="ORD-VC-1",
        retailer_id=retailer["id"],
        order_date=date(2026, 6, 1),
        requested_delivery_date=date(2026, 6, 15),
        required_ship_date=date(2026, 6, 10),
    )
    repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=38000,
        unit_price=5.0,
        material_id=material["id"],
        plant_id=plant["id"],
    )

    service = _build_service(repos)

    commitment_projections = service.run_commitment_projection(agreement_id, as_of_date=as_of_date)
    assert len(commitment_projections) == 1
    assert commitment_projections[0].projected_shortfall_quantity == pytest.approx(12000.0)

    per_po_result = service.run_for_purchase_order(purchase_order["id"], projection_date=as_of_date)
    assert per_po_result.violations == []
    assert len(per_po_result.skipped) == 1
    assert per_po_result.skipped[0].violation_type == "VOLUME_SHORTFALL"


def test_project_full_window_total_extrapolates_from_partial_elapsed_window():
    # Hand-computable from commitment.py's own docstring: 100 units in the first 10 days
    # of a 40-day window -> 100/10*40 = 400 units for the full window.
    assert project_full_window_total(actual_to_date=100.0, elapsed_days=10, total_window_days=40) == 400.0


def test_compute_commitment_shortfall_probability_hand_computed():
    # committed=50,000, projected=38,000 -> (50,000-38,000)/50,000 = 0.24.
    probability = compute_commitment_shortfall_probability(committed=50000.0, projected_total=38000.0)
    assert probability == pytest.approx(0.24)


def test_run_commitment_projection_requires_retailer_agreements_repository(repos):
    from app.core.exceptions import BusinessRuleError

    service = ProjectionService(
        purchase_orders=repos.purchase_orders,
        fulfillment=repos.fulfillment,
        rules=repos.penalty_rules,
        master_data=repos.master_data,
        projections=repos.penalty_projections,
    )
    retailer = repos.master_data.add_retailer("RET-VC-2", "No Commitment Repo Co", None)
    agreement_id = make_retailer_agreement(repos, retailer["id"])

    with pytest.raises(BusinessRuleError) as exc_info:
        service.run_commitment_projection(retailer_agreement_id=agreement_id)
    assert exc_info.value.code == "COMMITMENT_PROJECTION_NOT_CONFIGURED"
