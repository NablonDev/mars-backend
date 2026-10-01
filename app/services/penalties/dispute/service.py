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

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from uuid import UUID

from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.models.enums import DisputeStatus
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
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
from app.utils.clock import business_today, utc_now

_TERMINAL_STATUSES = {DisputeStatus.RESOLVED, DisputeStatus.OVERRIDDEN}

# The charge's own status (`actual_penalty.dispute_status`) follows its dispute, because the fines
# list and KPIs read it: DISPUTED while a dispute is open, then the outcome once it is decided.
# A partial reduction has no status of its own, so PAY_PARTIAL maps to UPHELD; the recoverable
# amount lives on the dispute row (`delta_amount`).
_CHARGE_STATUS_DISPUTED = "DISPUTED"
_CHARGE_STATUS_BY_VERDICT = {"NO_PAY": "WAIVED", "PAY_PARTIAL": "UPHELD", "PAY_FULL": "UPHELD"}

# Events worth surfacing as delivery proof in a dispute's analysis breakdown.
_PROOF_EVENT_TYPES = (
    "GATE_ARRIVAL",
    "DELIVERY_COMPLETED",
    "CARRIER_STATUS_UPDATE",
    "ASN_SENT",
    "CARRIER_APPOINTMENT_CONFIRMED",
    "PLAN_CHANGED",
    "REJECTED",
)
_PROOF_COMPLETED_MILESTONES = ("DELIVERED", "ASN_SENT", "LOADED", "GOODS_ISSUED")


@dataclass
class _TimelineFacts:
    """Delivery facts aggregated across every non-cancelled fulfillment plan of one PO.

    `None` means no fact is on record, never a confirmed zero. `delivered_qty` is shipped
    quantity summed over all plans (a split PO has several); `actual_delivery_date` is the
    latest actual measured-milestone date across them.
    """

    delivered_qty: float | None = None
    actual_delivery_date: date | None = None
    timeline_target_date: date | None = None
    asn_sent_date: date | None = None
    goods_issued_date: date | None = None
    telematics_events: list[dict] = field(default_factory=list)


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
    projection_service: ProjectionService | None = None
    fulfillment: FulfillmentRepository | None = None
    fulfillment_timeline: FulfillmentTimelineRepository | None = None
    # Only needed for a VOLUME_COMMITMENT dispute's actual-purchase-quantity lookup; every
    # other family works without it.
    retailer_agreements: RetailerAgreementRepository | None = None
    # Fallback response window (days) when no retailer agreement carries its own
    # dispute_window_days, or none is currently effective. Overridden by
    # Settings.dispute.default_window_days at DI wiring time; the default here
    # only matters for a service built directly (e.g. in tests).
    default_window_days: int = 90

    def open_dispute(
        self,
        actual_penalty_id: UUID,
        reason_code: str,
        claimed_amount: float,
        now_date: date | None = None,
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
        different facts requires a new charge, not a second write here.

        `response_due_date` is set from the retailer agreement effective on the
        purchase order's retailer as of the open date, if one carries a
        `dispute_window_days`; otherwise it falls back to `default_window_days`.
        `now_date` pins the open date for tests, the same "injectable for tests"
        purpose `now` serves on `analyze()`/`resolve()`.
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
        purchase_order = self.purchase_orders.require_purchase_order(purchase_order_id)

        if claim_facts is not None:
            try:
                self.actual_penalties.set_claim_facts(actual_penalty_id, claim_facts)
            except ValueError as exc:
                raise ConflictError(
                    code="CLAIM_FACTS_ALREADY_SET",
                    message=(f"claim_facts already recorded for actual_penalty {actual_penalty_id}: {exc}"),
                ) from exc

        open_date = now_date or business_today()
        response_due_date = open_date + timedelta(days=self.default_window_days)
        if self.retailer_agreements is not None:
            effective_agreement = self.retailer_agreements.get_effective_for_retailer(
                purchase_order["retailer_id"], open_date
            )
            if effective_agreement is not None and effective_agreement.get("dispute_window_days"):
                response_due_date = open_date + timedelta(days=effective_agreement["dispute_window_days"])

        dispute = self.disputes.create(
            actual_penalty_id=actual_penalty_id,
            purchase_order_id=purchase_order_id,
            reason_code=reason_code,
            claimed_amount=claimed_amount,
            notes=notes,
            response_due_date=response_due_date,
        )
        self.actual_penalties.set_dispute_status(actual_penalty_id, _CHARGE_STATUS_DISPUTED)
        return dispute

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

        # Facts come from every non-cancelled plan of the PO (a split PO has several).
        timeline_facts = self._resolve_timeline_facts(dispute["purchase_order_id"], purchase_order)
        required_delivery_date = self._resolve_required_delivery_date(purchase_order, timeline_facts)

        # The contract version in force on the delivery due date governs the obligation,
        # not the date the retailer happened to deduct. The deduction date is the fallback.
        lookup_dates = [d for d in (required_delivery_date, as_of_date) if d is not None]
        matching = self._matching_rules(
            purchase_order["retailer_id"], actual_penalty["violation_type"], lookup_dates
        )
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

        # Resolve order lines and header facts directly from PurchaseOrderRepository
        lines = self.purchase_orders.list_lines(dispute["purchase_order_id"])
        if not lines:
            raise BusinessRuleError(
                code="NO_ACTIVE_RULES",
                message=f"Purchase order {dispute['purchase_order_id']} has no lines to dispute.",
            )
        order_qty = round(sum(line["ordered_quantity"] for line in lines))
        unit_price = (
            sum(line["ordered_quantity"] * line["unit_price"] for line in lines) / order_qty
            if order_qty > 0
            else 0.0
        )
        delivered_qty = timeline_facts.delivered_qty
        actual_delivery_date = timeline_facts.actual_delivery_date
        telematics_events = timeline_facts.telematics_events

        if required_delivery_date is None:
            raise BusinessRuleError(
                code="INVALID_PURCHASE_ORDER",
                message=f"Purchase order {dispute['purchase_order_id']} is missing delivery dates.",
            )

        # Fall back to legacy fulfillment repository if not resolved from timeline
        fulfillment = self.fulfillment or (
            self.projection_service.fulfillment if self.projection_service else None
        )
        if fulfillment is not None:
            if delivered_qty is None:
                delivered_qty = fulfillment.get_delivered_quantity_for_purchase_order_not_after(
                    dispute["purchase_order_id"], as_of_date
                )
            if actual_delivery_date is None:
                shipment = fulfillment.get_latest_shipment_for_purchase_order_not_after(
                    dispute["purchase_order_id"], as_of_date
                )
                actual_delivery_date = shipment["actual_delivery_date"] if shipment is not None else None

        # claim_facts is the only claim-supplied input; every other DisputeFacts field
        # below is Mars-derived (from the lines, the rule row, or a repository), never
        # read from claim_facts even when one happens to carry a same-named key.
        claim_facts = actual_penalty.get("claim_facts") or {}
        actual_purchase_quantity = None
        if rule_value.engine_family == ENGINE_FAMILY_VOLUME_COMMITMENT:
            actual_purchase_quantity = self._resolve_commitment_actual_purchase_quantity(
                rule_row, purchase_order["retailer_id"], as_of_date
            )

        facts = DisputeFacts(
            order_qty=order_qty,
            unit_price=unit_price,
            delivered_qty=delivered_qty,
            required_delivery_date=required_delivery_date,
            actual_delivery_date=actual_delivery_date,
            asn_sent_date=timeline_facts.asn_sent_date,
            goods_issued_date=timeline_facts.goods_issued_date,
            grace_period_days=rule_row["grace_period_days"],
            defect_units=claim_facts.get("defect_units"),
            defect_rate_pct=claim_facts.get("defect_rate_pct"),
            replacement_cost_paid=claim_facts.get("replacement_cost_paid"),
            original_cost=order_qty * unit_price,
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
            "claim_supplied_keys": calc_trace.get("claim_supplied_keys", []),
            "mars_derived_keys": calc_trace.get("mars_derived_keys", []),
            "required_delivery_date": facts.required_delivery_date.isoformat(),
            "actual_delivery_date": (
                facts.actual_delivery_date.isoformat() if facts.actual_delivery_date is not None else None
            ),
            "deadline": deadline.isoformat() if deadline is not None else None,
            "grace_period_days": facts.grace_period_days,
            "shortfall_units": calc_trace.get("shortfall_units"),
            "telematics_events": telematics_events,
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
        resolved = self.disputes.resolve(
            dispute_id,
            dispute_status=status,
            resolved_by=resolved_by,
            resolved_at=now or utc_now(),
            override_verdict=override_verdict,
            override_reason=override_reason,
        )
        final_verdict = override_verdict or dispute["verdict"]
        charge_status = _CHARGE_STATUS_BY_VERDICT.get(str(getattr(final_verdict, "value", final_verdict)))
        if charge_status is not None:
            self.actual_penalties.set_dispute_status(dispute["actual_penalty_id"], charge_status)
        return resolved

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

    def _resolve_timeline_facts(self, purchase_order_id: UUID, purchase_order: dict) -> _TimelineFacts:
        """Aggregate delivery, ASN, goods-issue facts and proof events over all non-cancelled plans.

        Shipped quantity is summed across plans and the latest actual date wins, so a split
        PO is judged on its total delivery, not on its first plan alone. Confirmed quantity
        is a pre-delivery promise and is never used as a delivered quantity.
        """
        facts = _TimelineFacts()
        timeline = self.fulfillment_timeline
        if timeline is None:
            return facts
        plans = [
            plan
            for plan in timeline.list_plans_for_purchase_order(purchase_order_id)
            if plan.get("status") != "CANCELLED"
        ]

        shipped_total = 0.0
        delivery_dates: list[date] = []
        asn_dates: list[date] = []
        goods_issued_dates: list[date] = []
        for plan in plans:
            milestones = timeline.list_milestones(plan["id"])
            freight_term = (
                plan.get("freight_term") or purchase_order.get("freight_term") or "PREPAID"
            ).upper()
            target_code = "READY_FOR_PICKUP" if freight_term == "COLLECT" else "DELIVERED"

            actual_target = self._actual_date(milestones, target_code) or self._actual_date(
                milestones, "DELIVERED"
            )
            if actual_target is not None:
                delivery_dates.append(actual_target)
            asn_sent = self._actual_date(milestones, "ASN_SENT")
            if asn_sent is not None:
                asn_dates.append(asn_sent)
            goods_issued = self._actual_date(milestones, "GOODS_ISSUED")
            if goods_issued is not None:
                goods_issued_dates.append(goods_issued)

            if facts.timeline_target_date is None:
                ref_milestone = next((m for m in milestones if m["code"] == target_code), None) or next(
                    (m for m in milestones if m["code"] == "DELIVERED"), None
                )
                if ref_milestone is not None:
                    facts.timeline_target_date = ref_milestone.get("baseline_date") or ref_milestone.get(
                        "planned_date"
                    )

            for line in timeline.list_plan_lines(plan["id"]):
                shipped_total += float(line.get("shipped_quantity") or 0)

            ms_type_to_code = {
                m["milestone_type_id"]: m["code"]
                for m in milestones
                if "milestone_type_id" in m and "code" in m
            }
            facts.telematics_events.extend(self._proof_events(timeline, plan["id"], ms_type_to_code))

        facts.delivered_qty = shipped_total if shipped_total > 0 else None
        facts.actual_delivery_date = max(delivery_dates, default=None)
        facts.asn_sent_date = max(asn_dates, default=None)
        facts.goods_issued_date = max(goods_issued_dates, default=None)
        return facts

    def _actual_date(self, milestones: list[dict], code: str) -> date | None:
        """The `actual_date` of milestone `code`, or None if absent or not yet completed."""
        milestone = next((m for m in milestones if m["code"] == code and m["actual_date"] is not None), None)
        return milestone["actual_date"] if milestone is not None else None

    def _proof_events(
        self, timeline: FulfillmentTimelineRepository, plan_id: UUID, ms_type_to_code: dict
    ) -> list[dict]:
        """Delivery-proof events for one plan, shaped for the dispute's analysis breakdown."""
        proof: list[dict] = []
        for ev in timeline.list_events(plan_id):
            ms_code = ms_type_to_code.get(ev.get("milestone_type_id"))
            ev_type = ev.get("event_type")
            is_proof = (
                ev_type in _PROOF_EVENT_TYPES
                or ev.get("reason_code")
                or (ev_type == "COMPLETED" and ms_code in _PROOF_COMPLETED_MILESTONES)
            )
            if not is_proof:
                continue
            ev_at = ev.get("event_at")
            event_at_str = (
                ev_at.isoformat()
                if ev_at is not None and hasattr(ev_at, "isoformat")
                else (str(ev_at) if ev_at is not None else None)
            )
            label = (
                f"{ms_code}_{ev_type}" if ms_code and ev_type in ("COMPLETED", "PLAN_CHANGED") else ev_type
            )
            proof.append(
                {
                    "event_type": label,
                    "event_at": event_at_str,
                    "source": ev.get("source"),
                    "source_reference": ev.get("source_reference"),
                    "reason_code": ev.get("reason_code"),
                }
            )
        return proof

    def _resolve_required_delivery_date(self, purchase_order: dict, facts: _TimelineFacts) -> date | None:
        """The retailer's delivery due date for this PO, or None when no date is on record."""
        raw = (
            purchase_order.get("current_delivery_date")
            or purchase_order.get("requested_delivery_date")
            or purchase_order.get("window_end")
            or purchase_order.get("window_start")
            or facts.timeline_target_date
        )
        if raw is None:
            return None
        return date.fromisoformat(raw) if isinstance(raw, str) else raw

    def _matching_rules(self, retailer_id: UUID, violation_type: str, lookup_dates: list[date]) -> list[dict]:
        """Rules of `violation_type` effective on the first lookup date that has any; [] if none do."""
        for lookup_date in lookup_dates:
            candidates = self.rules.list_rules_effective_on(retailer_id, lookup_date)
            matching = [r for r in candidates if r["violation_type"] == violation_type]
            if matching:
                return matching
        return []

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
