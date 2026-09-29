"""Orchestrates idempotent seeding of master data, rules, and scenarios."""

from __future__ import annotations

from dataclasses import dataclass

from app.repositories.common.delivery_change_request import PoDeliveryChangeRequestRepository
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.dispute import PenaltyDisputeRepository
from app.repositories.penalties.job_context import (
    PenaltyJobItemContextRepository,
    PenaltyJobRunContextRepository,
)
from app.repositories.penalties.mitigation import MitigationInputRepository
from app.repositories.penalties.projection import ActualPenaltyRepository, PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.penalties.delivery_change import PoDeliveryChangeRequestService
from app.services.penalties.projection.service import ProjectionService
from app.services.seeding import dispute as dispute_seed
from app.services.seeding import master_data as master_data_seed
from app.services.seeding import mitigation as mitigation_seed
from app.services.seeding import projection as projection_seed


@dataclass
class PenaltySeedingService:
    """Idempotent seeding and day-by-day scenario replay for the penalties domain."""

    master_data: MasterDataRepository
    retailer_agreements: RetailerAgreementRepository
    rules: PenaltyRuleRepository
    purchase_orders: PurchaseOrderRepository
    fulfillment: FulfillmentRepository
    projection_service: ProjectionService
    mitigation_inputs: MitigationInputRepository
    delivery_change_service: PoDeliveryChangeRequestService
    delivery_change_requests: PoDeliveryChangeRequestRepository
    penalty_summaries: PenaltySummaryRepository
    penalty_projections: PenaltyProjectionRepository
    actual_penalties: ActualPenaltyRepository
    disputes: PenaltyDisputeRepository
    job_queue: JobQueueRepository
    penalty_job_item_context: PenaltyJobItemContextRepository
    penalty_job_run_context: PenaltyJobRunContextRepository

    def seed_master_data(self, force: bool = False) -> dict:
        """Seed master data, rules, and worked-example fixtures, skipping what already exists.

        `force=True` truncates every seeded table first and reseeds from scratch (a full
        reset, not a per-field upsert). Intended for demo/seed environments only.
        """
        if force:
            self._truncate_seeded_tables()

        counts: dict[str, int] = {}
        counts.update(master_data_seed.seed(self.master_data, self.retailer_agreements))
        counts.update(
            projection_seed.seed(
                self.rules,
                self.purchase_orders,
                self.fulfillment,
                self.master_data,
                self.retailer_agreements,
            )
        )
        counts.update(mitigation_seed.seed(self.mitigation_inputs, self.purchase_orders))
        counts.update(self.seed_disputes())
        return counts

    def seed_disputes(self) -> dict[str, int]:
        """Seed additive dispute fixtures, leaving the four worked-example POs untouched.

        Idempotent, like every other `seed_*` step, and callable on its own for tests
        that need only these fixtures.
        """
        return dispute_seed.seed(
            self.rules,
            self.purchase_orders,
            self.fulfillment,
            self.master_data,
            self.actual_penalties,
            self.retailer_agreements,
        )

    def _truncate_seeded_tables(self) -> None:
        """Truncate every seeded table in FK-safe order: children before the parents they reference.

        Call order is load-bearing; see `docs/DATABASE.md` for the FK graph.
        """
        self.penalty_summaries.truncate_all()
        self.penalty_job_item_context.truncate_all()
        self.disputes.truncate_all()
        self.penalty_job_run_context.truncate_all()
        self.mitigation_inputs.truncate_all()  # also clears mitigation_option
        self.delivery_change_requests.truncate_all()
        self.penalty_projections.truncate_all()
        self.actual_penalties.truncate_all()
        self.job_queue.truncate_all()
        self.rules.truncate_all()
        self.retailer_agreements.truncate_all()
        self.fulfillment.truncate_all()
        self.purchase_orders.truncate_all()
        self.master_data.truncate_all()

    def simulate_daily_run(self) -> list[dict]:
        """Replay all four scenarios day by day, writing facts and running projections.

        Marks each order DELIVERED after its final day, and interleaves one PO
        delivery-change-request negotiation outcome per order.
        """
        return projection_seed.simulate_daily_run(
            self.purchase_orders,
            self.fulfillment,
            self.master_data,
            self.projection_service,
            self.delivery_change_service,
        )
