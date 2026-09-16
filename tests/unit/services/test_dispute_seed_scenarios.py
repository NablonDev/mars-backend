"""Runs the eight dispute seed scenarios
(`app.services.seeding.scenario_data_dispute.SCENARIOS`) end to end --
`PenaltySeedingService.seed_disputes()` -> `DisputeResolutionService.open_dispute()`
-> `.analyze()` -- against the SQLite test DB, and asserts each one
produces exactly the verdict/computed_amount/delta_amount (or error code)
its fixture declares. This is the "confirm the new seed scenarios actually
produce the expected verdicts" verification step -- not just asserted, run.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import AppError
from app.services.penalties.dispute.service import DisputeResolutionService
from app.services.penalties.projection.service import ProjectionService
from app.services.seeding.scenario_data_dispute import SCENARIOS


@pytest.fixture
def dispute_service(repos) -> DisputeResolutionService:
    projection_service = ProjectionService(
        purchase_orders=repos.purchase_orders,
        fulfillment=repos.fulfillment,
        rules=repos.penalty_rules,
        master_data=repos.master_data,
        projections=repos.penalty_projections,
    )
    return DisputeResolutionService(
        purchase_orders=repos.purchase_orders,
        disputes=repos.disputes,
        actual_penalties=repos.actual_penalties,
        rules=repos.penalty_rules,
        projection_service=projection_service,
    )


@pytest.fixture(autouse=True)
def _seed(repos):
    from app.services.seeding import dispute as dispute_seed

    counts = dispute_seed.seed(
        repos.penalty_rules,
        repos.purchase_orders,
        repos.fulfillment,
        repos.master_data,
        repos.actual_penalties,
        repos.retailer_agreements,
    )
    assert counts["dispute_rules"] == 5
    assert counts["dispute_orders"] == len(SCENARIOS)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
def test_seed_scenario_produces_expected_verdict(scenario, repos, dispute_service):
    purchase_order = repos.purchase_orders.get_by_number(scenario.purchase_order_number)
    actual_penalty = repos.actual_penalties.list_for_purchase_order(purchase_order["id"])[0]

    dispute = dispute_service.open_dispute(actual_penalty["id"], "AMOUNT_INCORRECT", scenario.claimed_amount)

    if scenario.expected_error_code is not None:
        with pytest.raises(AppError) as exc_info:
            dispute_service.analyze(dispute["id"])
        assert exc_info.value.code == scenario.expected_error_code
        # No partial write on error -- the dispute stays OPEN.
        unchanged = dispute_service.get(dispute["id"])
        assert unchanged["dispute_status"] == "OPEN"
        return

    analyzed = dispute_service.analyze(dispute["id"])
    assert analyzed["verdict"] == scenario.expected_verdict
    if scenario.expected_computed_amount is not None:
        assert analyzed["computed_amount"] == pytest.approx(scenario.expected_computed_amount, abs=0.01)
    if scenario.expected_delta_amount is not None:
        assert analyzed["delta_amount"] == pytest.approx(scenario.expected_delta_amount, abs=0.01)
