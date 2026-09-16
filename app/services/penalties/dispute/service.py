"""Orchestrates the dispute lifecycle: open, analyze, resolve or override, and read.

Entry points:
    open_dispute (POST /api/v1/penalties/disputes)
    list_for_purchase_order (GET /api/v1/penalties/disputes)
    get (GET /api/v1/penalties/disputes/{dispute_id})
    analyze (POST /api/v1/penalties/disputes/{dispute_id}/analyze)
    resolve (POST /api/v1/penalties/disputes/{dispute_id}/resolve)

`analyze()` is synchronous, with no LangGraph and no job queue, per a locked
design decision: a human never blocks on approval before a verdict is written,
and resolves or overrides it afterward through a plain API call. The shape
mirrors `PoDeliveryChangeRequestService`, a dataclass of injected dependencies
with one method per lifecycle transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.models.enums import DisputeStatus
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.dispute import PenaltyDisputeRepository
from app.repositories.penalties.projection import ActualPenaltyRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.services.penalties.dispute.engine import recompute_dispute
from app.services.penalties.dispute.types import (
    DisputeFacts,
    InsufficientDataForDisputeError,
    UnsupportedDisputeCalcError,
)
from app.services.penalties.projection.commitment import resolve_measurement_window
from app.services.penalties.projection.service import ProjectionService
from app.services.penalties.projection.types import ENGINE_FAMILY_VOLUME_COMMITMENT
from app.utils.clock import utc_now

_TERMINAL_STATUSES = {DisputeStatus.RESOLVED, DisputeStatus.OVERRIDDEN}


@dataclass
class DisputeResolutionService:
    """Manages penalty disputes through their lifecycle.

    A dispute contests an `actual_penalty` charge: opened as OPEN, moved to
    ANALYZED once the engine has produced a verdict, then closed as RESOLVED or
    OVERRIDDEN. Analysis is synchronous, which leaves narrative generation as
    the only asynchronous work in the domain.
    """

    purchase_orders: PurchaseOrderRepository
    disputes: PenaltyDisputeRepository
    actual_penalties: ActualPenaltyRepository
    rules: PenaltyRuleRepository
    projection_service: ProjectionService
    # Only needed for a VOLUME_COMMITMENT dispute's actual-purchase-quantity lookup; every
    # other family works without it.
    retailer_agreements: RetailerAgreementRepository | None = None

    def open_dispute(
        self,
        actual_penalty_id: UUID,
        reason_code: str,
        claimed_amount: float,
        notes: str | None = None,
        claim_facts: dict | None = None,
    ) -> dict:
        """Open a new dispute against an already-recorded `actual_penalty` charge.

        At most one OPEN or ANALYZED dispute may exist per charge. A retailer
        amending a charge gets a second dispute cycle only once the first has
        reached a terminal status.

        `claim_facts` (validated at the schema layer, see
        `app.schemas.penalties.disputes.ClaimFacts`) is written once onto the charge's
        `actual_penalty` row, never mutated after: raises `ConflictError` if this charge
        already carries claim_facts from an earlier dispute cycle. A charge needing
        different facts requires a new charge, not a second write here (decision #3,
        docs/architecture/extraction-engine-integration-plan.md Phase 4).
        """
        actual_penalty = self.actual_penalties.get(actual_penalty_id)
        if actual_penalty is None:
            raise NotFoundError(
                code="ACTUAL_PENALTY_NOT_FOUND",
                message=f"No actual penalty found with actual_penalty_id={actual_penalty_id}",
            )

        active = self.disputes.find_active_for_actual_penalty(actual_penalty_id)
        if active is not None:
            raise ConflictError(
                code="ACTIVE_DISPUTE_EXISTS",
                message=(
                    f"Actual penalty {actual_penalty_id} already has an active "
                    f"(OPEN/ANALYZED) dispute ({active['id']})."
                ),
            )

        purchase_order_id = actual_penalty["purchase_order_id"]
        self.purchase_orders.require_purchase_order(purchase_order_id)

        if claim_facts is not None:
            try:
                self.actual_penalties.set_claim_facts(actual_penalty_id, claim_facts)
            except ValueError as exc:
                raise ConflictError(
                    code="CLAIM_FACTS_ALREADY_SET",
                    message=(f"claim_facts already recorded for actual_penalty {actual_penalty_id}: {exc}"),
                ) from exc

        return self.disputes.create(
            actual_penalty_id=actual_penalty_id,
            purchase_order_id=purchase_order_id,
            reason_code=reason_code,
            claimed_amount=claimed_amount,
            notes=notes,
        )

    def analyze(self, dispute_id: UUID, now: datetime | None = None) -> dict:
        """Compute and persist the deterministic verdict, moving the dispute to ANALYZED.

        Facts and the effective rule are resolved as of the historical charge
        date, not today. Every failure path raises before any write, so a failed
        analysis leaves the dispute untouched.

        Re-analyzable while OPEN or already ANALYZED, for instance after a rule
        record is corrected. Refused once RESOLVED or OVERRIDDEN: a human
        decision is already recorded on top of a verdict, and recomputing
        underneath it would strand that decision against a different verdict.
        """
        dispute = self._require_dispute(dispute_id)
        if dispute["dispute_status"] in _TERMINAL_STATUSES:
            raise ValidationError(
                code="DISPUTE_ALREADY_RESOLVED",
                message=(
                    f"Dispute {dispute_id} is already {dispute['dispute_status']} and cannot be re-analyzed."
                ),
            )

        actual_penalty = self.actual_penalties.get(dispute["actual_penalty_id"])
        if actual_penalty is None:
            raise NotFoundError(
                code="ACTUAL_PENALTY_NOT_FOUND",
                message=f"No actual penalty found with actual_penalty_id={dispute['actual_penalty_id']}",
            )
        purchase_order = self.purchase_orders.require_purchase_order(dispute["purchase_order_id"])
        as_of_date = actual_penalty["invoice_or_deduction_date"]

        candidates = self.rules.list_rules_effective_on(purchase_order["retailer_id"], as_of_date)
        matching = [r for r in candidates if r["violation_type"] == actual_penalty["violation_type"]]
        if not matching:
            raise BusinessRuleError(
                code="NO_MATCHING_RULE_FOR_DISPUTE",
                message=(
                    f"No penalty rule was effective for retailer {purchase_order['retailer_id']}, "
                    f"violation_type={actual_penalty['violation_type']!r} on {as_of_date.isoformat()} "
                    f"(dispute {dispute_id})."
                ),
            )
        # Deterministic tie-break when more than one rule matched, which is not
        # expected: effective date ranges for the same retailer and
        # violation_type should never overlap. Most recently effective wins.
        rule_row = max(matching, key=lambda r: r["effective_start_date"])
        rule_value = self.rules.get_rule_value(rule_row["id"])

        fulfillment = self.projection_service.fulfillment
        # order_qty, unit_price, and required_delivery_date reuse the projection
        # engine's snapshot assembly, which is safe because a dispute always
        # runs after delivery is final. snapshot.confirmed_qty is deliberately
        # unused: it comes from order_confirmation, the pre-delivery promise,
        # not the final delivered quantity a dispute must adjudicate on.
        snapshot = self.projection_service.build_snapshot(dispute["purchase_order_id"], as_of_date)
        delivered_qty = fulfillment.get_delivered_quantity_for_purchase_order_not_after(
            dispute["purchase_order_id"], as_of_date
        )
        shipment = fulfillment.get_latest_shipment_for_purchase_order_not_after(
            dispute["purchase_order_id"], as_of_date
        )
        actual_delivery_date = shipment["actual_delivery_date"] if shipment is not None else None

        # claim_facts is the only claim-supplied input; every other DisputeFacts field
        # below is Mars-derived (from the snapshot, the rule row, or a repository), never
        # read from claim_facts even when one happens to carry a same-named key (decision
        # #2, docs/architecture/extraction-engine-integration-plan.md Phase 4).
        claim_facts = actual_penalty.get("claim_facts") or {}
        actual_purchase_quantity = None
        if rule_value.engine_family == ENGINE_FAMILY_VOLUME_COMMITMENT:
            actual_purchase_quantity = self._resolve_commitment_actual_purchase_quantity(
                rule_row, purchase_order["retailer_id"], as_of_date
            )

        facts = DisputeFacts(
            order_qty=snapshot.order_qty,
            unit_price=snapshot.unit_price,
            delivered_qty=delivered_qty,
            required_delivery_date=snapshot.requested_delivery_date,
            actual_delivery_date=actual_delivery_date,
            grace_period_days=rule_row["grace_period_days"],
            defect_units=claim_facts.get("defect_units"),
            defect_rate_pct=claim_facts.get("defect_rate_pct"),
            replacement_cost_paid=claim_facts.get("replacement_cost_paid"),
            original_cost=snapshot.order_qty * snapshot.unit_price,
            occurrence_count=claim_facts.get("occurrence_count"),
            storage_days=claim_facts.get("storage_days"),
            committed_quantity=rule_value.commitment_quantity,
            actual_purchase_quantity=actual_purchase_quantity,
        )

        try:
            calculation = recompute_dispute(rule_value, facts, dispute["claimed_amount"])
        except InsufficientDataForDisputeError as exc:
            raise BusinessRuleError(
                code="INSUFFICIENT_DATA_FOR_DISPUTE",
                message=f"Cannot adjudicate dispute {dispute_id}: {exc}",
            ) from exc
        except UnsupportedDisputeCalcError as exc:
            raise BusinessRuleError(
                code="DISPUTE_CALC_NOT_SUPPORTED",
                message=f"Cannot adjudicate dispute {dispute_id}: {exc}",
            ) from exc

        calc_trace = calculation.calc_trace
        deadline = calc_trace.get("deadline")
        breakdown = {
            "rule_id": str(rule_row["id"]),
            "rule_code": rule_row["rule_code"],
            "calc_type": rule_row["calc_type"],
            "violation_family": calc_trace.get("violation_family"),
            "as_of_date": as_of_date.isoformat(),
            # Audit trail (4g) proving the claim-supplied/Mars-derived authority boundary:
            # which DisputeFacts keys this specific dispute's measure function read from
            # the claim vs from Mars's own records. See DisputeFamilyKeys/
            # FAMILY_REQUIRED_KEYS in app.services.penalties.dispute.types.
            "claim_supplied_keys": calc_trace.get("claim_supplied_keys", []),
            "mars_derived_keys": calc_trace.get("mars_derived_keys", []),
            "facts": {
                "order_qty": facts.order_qty,
                "unit_price": facts.unit_price,
                "delivered_qty": facts.delivered_qty,
                "shortfall_units": calc_trace.get("shortfall_units"),
                "required_delivery_date": facts.required_delivery_date.isoformat(),
                "actual_delivery_date": (
                    facts.actual_delivery_date.isoformat() if facts.actual_delivery_date is not None else None
                ),
                "deadline": deadline.isoformat() if deadline is not None else None,
                "is_late": calc_trace.get("is_late"),
                "grace_period_days": facts.grace_period_days,
                "defect_units": facts.defect_units,
                "defect_rate_pct": facts.defect_rate_pct,
                "replacement_cost_paid": facts.replacement_cost_paid,
                "original_cost": facts.original_cost,
                "occurrence_count": facts.occurrence_count,
                "storage_days": facts.storage_days,
                "committed_quantity": facts.committed_quantity,
                "actual_purchase_quantity": facts.actual_purchase_quantity,
            },
            "cap_amount": calc_trace.get("cap_amount"),
            "cap_applied": calc_trace.get("cap_applied"),
            "claimed_amount": calculation.claimed_amount,
            "computed_amount": calculation.computed_amount,
            "delta_amount": calculation.delta_amount,
        }

        return self.disputes.save_verdict(
            dispute_id,
            rule_id=rule_row["id"],
            computed_amount=calculation.computed_amount,
            delta_amount=calculation.delta_amount,
            verdict=calculation.verdict.value,
            analysis_breakdown=breakdown,
            analyzed_at=now or utc_now(),
        )

    def resolve(
        self,
        dispute_id: UUID,
        resolved_by: str,
        override_verdict: str | None = None,
        override_reason: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Close out a dispute; requires ANALYZED status.

        `override_verdict` set moves it to OVERRIDDEN and requires
        `override_reason`; otherwise it becomes RESOLVED, accepting the engine's
        own verdict unchanged.
        """
        dispute = self._require_dispute(dispute_id)
        if dispute["dispute_status"] != DisputeStatus.ANALYZED:
            raise ValidationError(
                code="DISPUTE_NOT_ANALYZED",
                message=(
                    f"Dispute {dispute_id} is not ANALYZED (status={dispute['dispute_status']!r}); "
                    "run analyze() first."
                ),
            )
        if override_verdict is not None and not override_reason:
            raise ValidationError(
                code="OVERRIDE_REASON_REQUIRED",
                message="override_reason is required when override_verdict is set.",
            )

        status = DisputeStatus.OVERRIDDEN if override_verdict is not None else DisputeStatus.RESOLVED
        return self.disputes.resolve(
            dispute_id,
            dispute_status=status,
            resolved_by=resolved_by,
            resolved_at=now or utc_now(),
            override_verdict=override_verdict,
            override_reason=override_reason,
        )

    def get(self, dispute_id: UUID) -> dict:
        """Fetch one dispute by id, regardless of its lifecycle status."""
        return self._require_dispute(dispute_id)

    def list_for_purchase_order(self, purchase_order_id: UUID | None = None) -> list[dict]:
        """List disputes, optionally scoped to one purchase order; all statuses included."""
        return self.disputes.list_for_purchase_order(purchase_order_id)

    def _require_dispute(self, dispute_id: UUID) -> dict:
        """Fetch a dispute by id or raise `NotFoundError`, one shared shape for every caller."""
        dispute = self.disputes.get_by_id(dispute_id)
        if dispute is None:
            raise NotFoundError(
                code="DISPUTE_NOT_FOUND", message=f"No penalty dispute found with dispute_id={dispute_id}"
            )
        return dispute

    def _resolve_commitment_actual_purchase_quantity(
        self, rule_row: dict, retailer_id: UUID, as_of_date: date
    ) -> float | None:
        """Mars-derived actual-to-date purchase quantity for a VOLUME_COMMITMENT rule's window.

        Reuses `resolve_measurement_window` so the window anchors identically to
        `ProjectionService.run_commitment_projection`. Raises `BusinessRuleError` if this
        service was built without a `retailer_agreements` repository: a VOLUME_COMMITMENT
        dispute cannot be adjudicated without it, never silently priced off a claim-supplied
        purchase total instead (decision #2/4f).
        """
        if self.retailer_agreements is None:
            raise BusinessRuleError(
                code="COMMITMENT_DISPUTE_NOT_CONFIGURED",
                message="DisputeResolutionService was built without a retailer_agreements repository.",
            )
        agreement = self.retailer_agreements.get(rule_row["retailer_agreement_id"])
        contract_anchor_date = (agreement["effective_date"] if agreement else None) or as_of_date
        window_start, window_end = resolve_measurement_window(
            rule_row["measurement_window_type"] or "ROLLING",
            rule_row["measurement_window_length"] or 1,
            rule_row["measurement_window_unit"] or "YEARS",
            contract_anchor_date,
            as_of_date,
        )
        query_end = min(as_of_date, window_end)
        total_qty, _total_value = self.purchase_orders.get_ordered_totals_for_retailer_between(
            retailer_id, window_start, query_end
        )
        return total_qty
