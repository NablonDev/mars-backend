"""Orchestrates penalty-projection runs: assemble snapshot, load rules, run engine, persist.

Entry points: run_for_purchase_order (POST /api/v1/penalties/projections).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

from app.core.exceptions import BusinessRuleError, NotFoundError
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.services.penalties.projection.commitment import (
    commitment_shortfall_probability as _commitment_shortfall_probability,
)
from app.services.penalties.projection.commitment import (
    price_volume_shortfall,
    resolve_measurement_window,
)
from app.services.penalties.projection.engine import ProjectionEngine
from app.services.penalties.projection.types import (
    ENGINE_FAMILY_VOLUME_COMMITMENT,
    AppointmentStatus,
    CommitmentProjection,
    CommitmentSnapshot,
    OrderSnapshot,
    PenaltyRule,
    ProductionStatus,
    ProjectionResult,
)
from app.utils.clock import utc_today

if TYPE_CHECKING:
    from app.repositories.common.retailer_agreement import RetailerAgreementRepository
    from app.repositories.penalties.projection import PenaltyProjectionRepository
    from app.repositories.penalties.rule import PenaltyRuleRepository


@dataclass
class ProjectionService:
    """Orchestrates penalty-projection runs for a single purchase order.

    Assembles a snapshot of current order state (quantity, confirmed shipments,
    production/carrier status) from PO and fulfillment facts as-of a projection
    date, loads the applicable penalty rules, runs the projection engine, and
    persists the result. Two entry points: run_for_purchase_order (per-PO) and
    run_commitment_projection (per retailer agreement, contract-period grain)."""

    purchase_orders: PurchaseOrderRepository
    fulfillment: FulfillmentRepository
    rules: PenaltyRuleRepository
    master_data: MasterDataRepository
    projections: PenaltyProjectionRepository
    # Only needed by run_commitment_projection; every other entry point works without it.
    retailer_agreements: RetailerAgreementRepository | None = None

    def build_snapshot(self, purchase_order_id: UUID, projection_date: date) -> OrderSnapshot:
        """Assemble an OrderSnapshot from purchase-order lines and fulfillment facts."""
        purchase_order = self.purchase_orders.require_purchase_order(purchase_order_id)
        lines = self.purchase_orders.list_lines(purchase_order_id)
        if not lines:
            raise BusinessRuleError(
                code="NO_ACTIVE_RULES",
                message=f"Purchase order {purchase_order_id} has no lines to project a snapshot from.",
            )

        order_qty = sum(line["ordered_quantity"] for line in lines)
        if order_qty > 0:
            unit_price = sum(line["ordered_quantity"] * line["unit_price"] for line in lines) / order_qty
        else:
            unit_price = 0.0

        confirmed_qty = 0.0
        demand_exception_flagged = False
        for line in lines:
            latest_confirmation = self.fulfillment.get_latest_confirmation_line_not_after(
                line["id"], projection_date
            )
            if latest_confirmation is not None:
                confirmed_qty += latest_confirmation["confirmed_quantity"]
            if self.fulfillment.has_open_demand_exception_not_after(line["id"], projection_date):
                demand_exception_flagged = True

        production_status = ProductionStatus.ON_TRACK
        primary_line = lines[0]
        if primary_line["material_id"] is not None and primary_line["plant_id"] is not None:
            latest_schedule = self.fulfillment.get_latest_production_schedule_not_after(
                primary_line["material_id"], primary_line["plant_id"], projection_date
            )
            if latest_schedule is not None:
                production_status = ProductionStatus(latest_schedule["status"])

        expected_ship_date = None
        actual_ship_date = None
        appointment_status = AppointmentStatus.SCHEDULED
        expected_transit_days = 2
        carrier_reliability_score = 90.0
        shipment = self.fulfillment.get_latest_shipment_for_purchase_order_not_after(
            purchase_order_id, projection_date
        )
        if shipment is not None:
            expected_ship_date = shipment["expected_ship_date"]
            actual_ship_date = shipment["actual_ship_date"]
            if shipment["appointment_status"]:
                appointment_status = AppointmentStatus(shipment["appointment_status"])
            if shipment["expected_transit_days"] is not None:
                expected_transit_days = shipment["expected_transit_days"]
            if shipment["carrier_id"] is not None:
                carrier = self.master_data.get_carrier(shipment["carrier_id"])
                if carrier is not None:
                    carrier_reliability_score = carrier["historical_reliability_score"]

        requested_delivery_date = (
            purchase_order["current_delivery_date"] or purchase_order["requested_delivery_date"]
        )
        required_ship_date = (
            purchase_order["current_required_ship_date"] or purchase_order["required_ship_date"]
        )

        return OrderSnapshot(
            order_id=str(purchase_order_id),
            projection_date=projection_date,
            order_qty=round(order_qty),
            unit_price=unit_price,
            requested_delivery_date=requested_delivery_date,
            required_ship_date=required_ship_date,
            confirmed_qty=round(confirmed_qty),
            production_status=production_status,
            demand_exception_flagged=demand_exception_flagged,
            expected_ship_date=expected_ship_date,
            actual_ship_date=actual_ship_date,
            appointment_status=appointment_status,
            carrier_reliability_score=carrier_reliability_score,
            expected_transit_days=expected_transit_days,
        )

    def run_for_purchase_order(
        self,
        purchase_order_id: UUID,
        projection_date: date | None = None,
        stacking_mode_override: str | None = None,
    ) -> ProjectionResult:
        """Run projection for a purchase order as-of a date, persist, and return result.

        Assembles order snapshot as-of projection_date (or today if omitted), loads
        retailer's active penalty rules, projects via engine, and persists each
        violation row to penalty_projection. Returns ProjectionResult with violations
        stamped with their persisted IDs. stacking_mode_override (if given) overrides
        the retailer's configured stacking mode. Raises NotFoundError if PO doesn't
        exist or BusinessRuleError if no active rules exist for the retailer."""
        purchase_order = self.purchase_orders.get_purchase_order(purchase_order_id)
        if purchase_order is None:
            raise NotFoundError(
                code="PO_NOT_FOUND",
                message=f"No purchase order found with purchase_order_id={purchase_order_id}",
            )

        projection_date = projection_date or utc_today()
        snapshot = self.build_snapshot(purchase_order_id, projection_date)
        rule_list = self.rules.list_rules_for_retailer(purchase_order["retailer_id"])
        if not rule_list:
            raise BusinessRuleError(
                code="NO_ACTIVE_RULES",
                message=(
                    f"No active penalty rules for retailer {purchase_order['retailer_id']} "
                    f"(purchase_order {purchase_order_id})"
                ),
            )

        stacking_mode = stacking_mode_override or self.master_data.get_stacking_mode(
            purchase_order["retailer_id"]
        )
        result = ProjectionEngine().project(snapshot, rule_list, stacking_mode=stacking_mode)
        ids_by_rule_id = self.projections.save_result(purchase_order_id, result)
        # Stitch each violation's persisted `penalty_projection.id` back onto it
        # so `POST /penalties/projections` can return that row's id.
        # `save_result` returns a rule_id -> id mapping instead of mutating
        # `result`, keeping it independent of the pure-engine dataclass.
        for v in result.violations:
            v.id = ids_by_rule_id[v.rule_id]
        return result

    def run_for_all_open(
        self,
        projection_date: date | None = None,
        stacking_mode_override: str | None = None,
    ) -> list[ProjectionResult]:
        """Run and persist a projection for every purchase order currently OPEN.

        A `BusinessRuleError` from any single order propagates and aborts the
        rest of the batch. There is no partial-batch handling, so the calling
        worker decides how to retry.
        """
        results = []
        for purchase_order in self.purchase_orders.list_purchase_orders(order_status="OPEN"):
            results.append(
                self.run_for_purchase_order(purchase_order["id"], projection_date, stacking_mode_override)
            )
        return results

    def run_commitment_projection(
        self, retailer_agreement_id: UUID, as_of_date: date | None = None
    ) -> list[CommitmentProjection]:
        """Project volume-commitment shortfall for one retailer agreement, as of a date.

        Contract-period grain, separate from run_for_purchase_order/run_for_all_open:
        a volume commitment is a risk against the whole agreement, not one purchase
        order, so this never runs from the per-PO loop and a VOLUME_COMMITMENT rule is
        never priced there either (it isn't shortage- or delay-shaped, so it lands in
        that loop's own `skipped` list). On-demand only for now, not wired into the
        daily batch. Raises NotFoundError for an unknown retailer agreement and
        BusinessRuleError if this service was built without a retailer_agreements
        repository. Returns one CommitmentProjection per active VOLUME_COMMITMENT rule
        on the agreement (empty list if it has none).
        """
        if self.retailer_agreements is None:
            raise BusinessRuleError(
                code="COMMITMENT_PROJECTION_NOT_CONFIGURED",
                message="ProjectionService was built without a retailer_agreements repository.",
            )
        agreement = self.retailer_agreements.get(retailer_agreement_id)
        if agreement is None:
            raise NotFoundError(
                code="RETAILER_AGREEMENT_NOT_FOUND",
                message=f"No retailer agreement found with retailer_agreement_id={retailer_agreement_id}",
            )

        as_of_date = as_of_date or utc_today()
        rules = self.rules.list_rules_for_agreement_by_family(
            retailer_agreement_id, ENGINE_FAMILY_VOLUME_COMMITMENT
        )
        return [
            self._project_commitment_rule(rule, self._build_commitment_snapshot(agreement, rule, as_of_date))
            for rule in rules
        ]

    def _build_commitment_snapshot(
        self, agreement: dict, rule: PenaltyRule, as_of_date: date
    ) -> CommitmentSnapshot:
        """Resolve one rule's measurement window and its actual-to-date purchase totals.

        Falls back to `as_of_date` as the contract anchor when the agreement carries no
        `effective_date` (CONTRACT_YEAR/ANNIVERSARY then anchor on the projection date
        itself rather than failing outright). The purchase-history query is bounded by
        `min(as_of_date, window_end_date)`, never the raw window end: purchases after
        today cannot be known yet, even when the window itself extends into the future.
        """
        contract_anchor_date = agreement["effective_date"] or as_of_date
        window_start, window_end = resolve_measurement_window(
            rule.measurement_window_type or "ROLLING",
            rule.measurement_window_length or 1,
            rule.measurement_window_unit or "YEARS",
            contract_anchor_date,
            as_of_date,
        )
        query_end = min(as_of_date, window_end)
        total_qty, total_value = self.purchase_orders.get_ordered_totals_for_retailer_between(
            agreement["retailer_id"], window_start, query_end
        )
        return CommitmentSnapshot(
            retailer_agreement_id=str(agreement["id"]),
            window_start_date=window_start,
            window_end_date=window_end,
            as_of_date=as_of_date,
            committed_quantity=rule.commitment_quantity,
            committed_value=rule.commitment_value,
            actual_to_date_quantity=total_qty if rule.commitment_quantity is not None else None,
            actual_to_date_value=total_value if rule.commitment_value is not None else None,
        )

    def _project_commitment_rule(
        self, rule: PenaltyRule, snapshot: CommitmentSnapshot
    ) -> CommitmentProjection:
        """Price one VOLUME_COMMITMENT rule's snapshot into a probability-weighted `CommitmentProjection`."""
        from app.services.penalties.projection.commitment import (
            projected_shortfall_quantity,
            projected_shortfall_value,
        )

        probability = _commitment_shortfall_probability(snapshot)
        penalty_amount = price_volume_shortfall(rule, snapshot)
        return CommitmentProjection(
            rule_id=rule.rule_id,
            retailer_agreement_id=snapshot.retailer_agreement_id,
            shortfall_probability=round(probability, 4),
            projected_shortfall_quantity=projected_shortfall_quantity(snapshot),
            projected_shortfall_value=projected_shortfall_value(snapshot),
            penalty_amount=round(penalty_amount, 2),
            expected_penalty_amount=round(probability * penalty_amount, 2),
        )
