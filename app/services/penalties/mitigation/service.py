"""Orchestrates one mitigation-ranking run.

Entry points: run_for_purchase_order (POST /api/v1/penalties/mitigations).

Reloads the purchase order's already-persisted projection for the day, loads
cause and cost inputs, runs the pure engine, persists the ranked options, and
returns the result.

Shape mirrors `ProjectionService`, but mitigation evaluates against a
projection that has already run and been persisted; it never calls
`ProjectionEngine.project` itself, except indirectly through
`ProjectionService.build_snapshot` to rebuild the same `OrderSnapshot` the
day's projection was computed from. The pure-engine `ProjectionResult`
dataclass is reconstructed from the persisted rows so `MitigationEngine`,
typed against that dataclass rather than a repository dict, works unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.core.exceptions import BusinessRuleError, NotFoundError
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.mitigation import MitigationInputRepository, MitigationOptionRepository
from app.repositories.penalties.projection import PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.services.penalties.mitigation.engine import MitigationEngine
from app.services.penalties.mitigation.types import MitigationOption
from app.services.penalties.projection import (
    DELAY_VIOLATION_TYPES,
    SHORTAGE_VIOLATION_TYPES,
    ProjectionResult,
    ViolationProjection,
)
from app.services.penalties.projection.service import ProjectionService
from app.utils.clock import utc_today


def _build_projection_result(
    purchase_order_id: UUID,
    projection_date: date,
    stacking_mode: str,
    day_rows: list[dict],
) -> ProjectionResult:
    """Rebuild the pure-engine `ProjectionResult` from one day's persisted rows.

    The projection summary service performs the same reconstruction for its
    LLM-context shape, but needs a Pydantic context row rather than the
    dataclass, so the two are kept independent.
    """
    violations = [
        ViolationProjection(
            violation_type=row["violation_type"],
            rule_id=str(row["rule_id"]),
            probability=row["failure_probability"],
            penalty_amount=row["penalty_amount"],
            expected_penalty_amount=row["expected_penalty_amount"],
        )
        for row in day_rows
    ]

    if stacking_mode == "MAX":
        total = max((v.expected_penalty_amount for v in violations), default=0.0)
    else:
        total = sum(v.expected_penalty_amount for v in violations)

    shortage_probability = next(
        (r["failure_probability"] for r in day_rows if r["violation_type"] in SHORTAGE_VIOLATION_TYPES),
        0.0,
    )
    delay_probability = next(
        (r["failure_probability"] for r in day_rows if r["violation_type"] in DELAY_VIOLATION_TYPES),
        0.0,
    )
    days_to_delivery = day_rows[0]["days_to_delivery"] if day_rows else 0

    return ProjectionResult(
        order_id=str(purchase_order_id),
        projection_date=projection_date,
        days_to_delivery=days_to_delivery,
        shortage_probability=round(shortage_probability, 4),
        delay_probability=round(delay_probability, 4),
        violations=violations,
        total_expected_penalty_amount=round(total, 2),
        stacking_mode=stacking_mode,
    )


@dataclass
class MitigationService:
    """Evaluates mitigation actions against a single purchase order's projection.

    Mirrors `ProjectionService`'s structure but operates on already-persisted
    projection data, never re-running the projection engine itself.
    """

    purchase_orders: PurchaseOrderRepository
    rules: PenaltyRuleRepository
    master_data: MasterDataRepository
    projections: PenaltyProjectionRepository
    mitigation_inputs: MitigationInputRepository
    mitigation_options: MitigationOptionRepository
    projection_service: ProjectionService

    def run_for_purchase_order(
        self,
        purchase_order_id: UUID,
        projection_date: date | None = None,
    ) -> tuple[date, list[MitigationOption]]:
        """Evaluate and persist mitigation actions for a purchase order's projection.

        `projection_date` defaults to today. Raises `NotFoundError` for an
        unknown purchase order and `BusinessRuleError` when no projection was
        persisted for that date, since there is then no baseline to rank against.
        """
        purchase_order = self.purchase_orders.get_purchase_order(purchase_order_id)
        if purchase_order is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id}",
            )

        projection_date = projection_date or utc_today()

        history = self.projections.list_history(purchase_order_id)
        day_rows = [row for row in history if row["projection_date"] == projection_date]
        if not day_rows:
            raise BusinessRuleError(
                code="NO_PROJECTION_EXISTS",
                message=(
                    f"No projection exists for purchase_order_id={purchase_order_id} on "
                    f"projection_date={projection_date.isoformat()}. Run "
                    "POST /penalties/projections for that date first."
                ),
            )

        # Current stacking mode, not whatever override produced the historical
        # projection, matching ProjectionService's own default path. Each
        # violation's expected_penalty_amount is computed independently of
        # stacking mode, so only the aggregate total is affected.
        stacking_mode = self.master_data.get_stacking_mode(purchase_order["retailer_id"])
        projection = _build_projection_result(purchase_order_id, projection_date, stacking_mode, day_rows)

        snapshot = self.projection_service.build_snapshot(purchase_order_id, projection_date)
        rule_list = self.rules.list_rules_for_retailer(purchase_order["retailer_id"])
        inputs = self.mitigation_inputs.get_inputs(purchase_order_id)

        options = MitigationEngine().evaluate(snapshot, rule_list, projection, inputs)
        self.mitigation_options.save_results(purchase_order_id, projection_date, options)
        self.mitigation_options.commit()
        return projection_date, options

    def get_latest(self, purchase_order_id: UUID) -> tuple[date, list[dict]]:
        """Latest persisted, ranked mitigation options for a purchase order."""
        purchase_order = self.purchase_orders.get_purchase_order(purchase_order_id)
        if purchase_order is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id}",
            )

        rows = self.mitigation_options.get_latest(purchase_order_id)
        if not rows:
            raise BusinessRuleError(
                code="NO_MITIGATION_OPTIONS_EXIST",
                message=(
                    f"No mitigation options exist yet for purchase_order_id={purchase_order_id}. "
                    "Run POST /penalties/mitigations first."
                ),
            )
        return rows[0]["projection_date"], rows
