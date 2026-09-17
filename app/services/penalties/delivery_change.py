"""Business logic for the PO delivery-date change request/response lifecycle.

A retailer's decision is recorded manually (no inbound webhook); every terminal
outcome re-runs `ProjectionService.run_for_purchase_order` same-day instead of
waiting for tomorrow's batch. `PurchaseOrder.negotiation_status` has exactly one
writer: this service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.repositories.common.delivery_change_request import PoDeliveryChangeRequestRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.services.penalties.projection.service import ProjectionService
from app.utils.clock import utc_now_naive
from app.utils.ids import new_id

_TERMINAL_DECISIONS = {"ACCEPTED", "COUNTERED", "REJECTED"}


@dataclass
class PoDeliveryChangeRequestService:
    """Orchestrates PO delivery-date change requests through their lifecycle.

    On terminal decision, `current_required_ship_date` shifts by the same delta as the
    delivery date to preserve the existing transit-day gap.
    """

    purchase_orders: PurchaseOrderRepository
    delivery_change_requests: PoDeliveryChangeRequestRepository
    projection_service: ProjectionService
    master_data: MasterDataRepository

    def create_request(
        self,
        purchase_order_id: UUID,
        reason_code: str,
        proposed_delivery_date: date,
        notes: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """`now` is injectable so scenario replay and tests can anchor timestamps to a mock as-of date."""
        purchase_order = self.purchase_orders.require_purchase_order(purchase_order_id)

        active = self.delivery_change_requests.find_active_for_purchase_order(purchase_order_id)
        if active is not None:
            raise ConflictError(
                code="ACTIVE_PO_DELIVERY_CHANGE_REQUEST_EXISTS",
                message=(
                    f"Purchase order {purchase_order_id} already has an active PO "
                    f"delivery-change request ({active['id']})"
                ),
            )

        policy = self.master_data.get_extension_policy(purchase_order["retailer_id"])

        now = now or utc_now_naive()
        current_required_ship_date = (
            purchase_order["current_required_ship_date"] or purchase_order["required_ship_date"]
        )
        lead_days = (current_required_ship_date - now.date()).days
        if lead_days < policy["min_lead_days"]:
            raise BusinessRuleError(
                code="PO_DELIVERY_CHANGE_LEAD_TIME_ERROR",
                message=(
                    f"Purchase order {purchase_order_id} is only {lead_days} day(s) from its required "
                    f"ship date ({current_required_ship_date.isoformat()}); minimum lead time to request "
                    f"a delivery-date change is {policy['min_lead_days']} day(s)."
                ),
            )

        baseline_delivery_date = (
            purchase_order["current_delivery_date"] or purchase_order["requested_delivery_date"]
        )

        created = self.delivery_change_requests.create(
            purchase_order_id=purchase_order_id,
            reason_code=reason_code,
            requested_at=now,
            baseline_delivery_date=baseline_delivery_date,
            proposed_delivery_date=proposed_delivery_date,
            expires_at=now + timedelta(hours=policy["response_sla_hours"]),
            # "ext" prefix: an external-system correlation key, generated here so every
            # request gets one regardless of entry point.
            request_id=new_id("ext"),
            notes=notes,
        )
        self.purchase_orders.update_negotiation_status(purchase_order_id, "PENDING")
        return created

    def record_response(
        self,
        delivery_change_request_id: UUID,
        decision: str,
        countered_delivery_date: date | None = None,
        now: datetime | None = None,
    ) -> dict:
        """`now` is injectable for the same reason as `create_request`'s."""
        row = self.delivery_change_requests.get_by_id(delivery_change_request_id)
        if row is None:
            raise NotFoundError(
                code="PO_DELIVERY_CHANGE_REQUEST_NOT_FOUND",
                message=f"No PO delivery-change request found with id={delivery_change_request_id}",
            )

        if row["status"] != "PENDING":
            raise ValidationError(
                code="INVALID_PO_DELIVERY_CHANGE_RESPONSE",
                message=(
                    f"PO delivery-change request {delivery_change_request_id} is not PENDING "
                    f"(status={row['status']!r}); a response has already been recorded, or it has "
                    "already expired."
                ),
            )

        if decision not in _TERMINAL_DECISIONS:
            raise ValidationError(
                code="INVALID_PO_DELIVERY_CHANGE_RESPONSE",
                message=f"decision must be one of {sorted(_TERMINAL_DECISIONS)}, got {decision!r}",
            )

        if decision == "COUNTERED":
            if countered_delivery_date is None:
                raise ValidationError(
                    code="INVALID_PO_DELIVERY_CHANGE_RESPONSE",
                    message="decision=COUNTERED requires countered_delivery_date",
                )
            if not (row["baseline_delivery_date"] < countered_delivery_date < row["proposed_delivery_date"]):
                raise ValidationError(
                    code="INVALID_PO_DELIVERY_CHANGE_RESPONSE",
                    message=(
                        "countered_delivery_date must fall strictly between "
                        f"baseline_delivery_date ({row['baseline_delivery_date'].isoformat()}) "
                        f"and proposed_delivery_date ({row['proposed_delivery_date'].isoformat()})"
                    ),
                )
        elif countered_delivery_date is not None:
            raise ValidationError(
                code="INVALID_PO_DELIVERY_CHANGE_RESPONSE",
                message="countered_delivery_date is only valid when decision=COUNTERED",
            )

        now = now or utc_now_naive()
        updated = self.delivery_change_requests.record_response(
            delivery_change_request_id=delivery_change_request_id,
            status=decision,
            retailer_response_date=now.date(),
            resolved_at=now,
            countered_delivery_date=countered_delivery_date,
        )

        purchase_order_id = row["purchase_order_id"]
        if decision in ("ACCEPTED", "COUNTERED"):
            if decision == "COUNTERED":
                assert countered_delivery_date is not None  # enforced by the COUNTERED branch above
                new_delivery_date = countered_delivery_date
            else:
                new_delivery_date = row["proposed_delivery_date"]
            # Shift required_ship_date by the same delta as the delivery-date change,
            # preserving the existing transit-day gap.
            delta = new_delivery_date - row["baseline_delivery_date"]
            purchase_order = self.purchase_orders.require_purchase_order(purchase_order_id)
            current_required_ship_date = (
                purchase_order["current_required_ship_date"] or purchase_order["required_ship_date"]
            )
            self.purchase_orders.update_current_dates(
                purchase_order_id,
                current_delivery_date=new_delivery_date,
                current_required_ship_date=current_required_ship_date + delta,
            )

        self.purchase_orders.update_negotiation_status(purchase_order_id, decision)
        self.projection_service.run_for_purchase_order(purchase_order_id, now.date())
        return updated

    def expire_stale(self, as_of: datetime | None = None) -> list[dict]:
        """Mark pending PO delivery-change requests as EXPIRED if past their SLA.

        Finds all PENDING requests whose SLA deadline has passed as of `as_of` (or
        wall-clock time if omitted), marks each as EXPIRED, updates the purchase
        order's negotiation_status to EXPIRED, and re-runs the projection with the
        unchanged delivery date (current_delivery_date was never touched while PENDING).
        Returns list of newly expired rows. Idempotent: subsequent calls find no newly
        expired rows and return empty list."""
        resolved_as_of = as_of or utc_now_naive()
        expired = self.delivery_change_requests.find_expired(resolved_as_of)

        results = []
        for row in expired:
            updated = self.delivery_change_requests.mark_expired(row["id"], resolved_as_of)
            # current_delivery_date is already untouched while PENDING; only negotiation_status changes.
            self.purchase_orders.update_negotiation_status(row["purchase_order_id"], "EXPIRED")
            self.projection_service.run_for_purchase_order(row["purchase_order_id"], resolved_as_of.date())
            results.append(updated)
        return results

    def list_history(self, purchase_order_id: UUID | None = None) -> list[dict]:
        """List delivery-change requests, scoped to one PO if given, or every PO otherwise."""
        return self.delivery_change_requests.list_history(purchase_order_id)

    def get_by_id(self, delivery_change_request_id: UUID) -> dict:
        """Fetch a single delivery-change request by its surrogate id."""
        row = self.delivery_change_requests.get_by_id(delivery_change_request_id)
        if row is None:
            raise NotFoundError(
                code="PO_DELIVERY_CHANGE_REQUEST_NOT_FOUND",
                message=f"No PO delivery-change request found with id={delivery_change_request_id}",
            )
        return row
