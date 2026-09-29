"""Penalty-projection job execution, recovery sweep, and daily enqueue.

Holds the ORDER_RUN and PROJECTION_SUMMARY_REGEN per-item work, the nightly enqueue of
one ORDER_RUN per OPEN purchase order, the recovery sweep for stranded PENDING
PROJECTION `penalty_summary` rows, and the PO delivery-change-request expiry sweep.

`ClaimedJob` carries no domain-shaped business key, so every per-item function below
re-reads `purchase_order_id`, `projection_date`, `stacking_mode_override` and
`force_regenerate_summary` from the matching `penalties.penalty_job_item_context` row,
keyed on `job.job_item_id`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import Settings
from app.db.session import Database
from app.models.enums import JobRunType, JobTaskType, SummaryStatus, SummaryType
from app.queue.interfaces import JobDispatcher
from app.queue.types import ClaimedJob, SweepResult
from app.repositories.common.delivery_change_request import PoDeliveryChangeRequestRepository
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository, describe_no_open_orders
from app.repositories.penalties.job_context import (
    PenaltyJobItemContextRepository,
    PenaltyJobRunContextRepository,
)
from app.repositories.penalties.projection import ActualPenaltyRepository, PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.penalties.delivery_change import PoDeliveryChangeRequestService
from app.services.penalties.projection.service import ProjectionService
from app.services.penalties.projection.summary_service import ProjectionSummaryService

logger = logging.getLogger(__name__)


def _missing_context_error(job_item_id: UUID) -> ValueError:
    """Build the error for a job item with no matching context row.

    A bare `ValueError` is what `classify_failure` dead-letters, and this can never
    succeed on retry.
    """
    return ValueError(
        f"No penalty_job_item_context found for job_item_id={job_item_id!r}; "
        "cannot execute this job without its purchase_order_id/projection_date."
    )


def _build_projection_service(session) -> ProjectionService:
    """Assemble a `ProjectionService` from a session-scoped set of repositories."""
    return ProjectionService(
        purchase_orders=PurchaseOrderRepository(session),
        fulfillment=FulfillmentRepository(session),
        rules=PenaltyRuleRepository(session),
        master_data=MasterDataRepository(session),
        projections=PenaltyProjectionRepository(session),
    )


def run_projection(job: ClaimedJob, database: Database) -> None:
    """Recompute and persist penalty projections for one purchase order.

    Raises a non-retryable `ValueError` when the job item's context row is missing.
    """
    with database.session() as session:
        context = PenaltyJobItemContextRepository(session).get(job.job_item_id)
        if context is None:
            raise _missing_context_error(job.job_item_id)

        service = _build_projection_service(session)
        service.run_for_purchase_order(
            context["purchase_order_id"],
            context["projection_date"],
            context["stacking_mode_override"],
        )


def run_summary(
    job: ClaimedJob,
    database: Database,
    llm: AzureOpenAIChatClient,
    *,
    heartbeat: Callable[[], None] | None,
) -> None:
    """Regenerate the LLM narrative over an existing projection.

    `heartbeat` is forwarded so the worker loop can detect lost ownership mid-call.
    The service only flushes its FAILED row, so a failure commits explicitly before
    re-raising; otherwise `Database.session()` would roll that row back before the
    worker loop could classify the failure.
    """
    # Keep the LLM call outside the projection session's lifetime.
    with database.session() as session:
        context = PenaltyJobItemContextRepository(session).get(job.job_item_id)
        if context is None:
            raise _missing_context_error(job.job_item_id)

        service = ProjectionSummaryService(
            purchase_orders=PurchaseOrderRepository(session),
            summaries=PenaltySummaryRepository(session),
            agent_registry=AgentRegistryRepository(session),
            job_queue=JobQueueRepository(session),
            job_context=PenaltyJobItemContextRepository(session),
            llm=llm,
            rules=PenaltyRuleRepository(session),
            master_data=MasterDataRepository(session),
            projections=PenaltyProjectionRepository(session),
            actual_penalties=ActualPenaltyRepository(session),
            projection_service=_build_projection_service(session),
        )
        summary_job = service.get_or_schedule(
            context["purchase_order_id"],
            as_of_date=context["projection_date"],
            force_regenerate=context["force_regenerate_summary"],
        )
        if summary_job.status == SummaryStatus.PENDING:
            # run_generation persists its FAILED row with a flush(), not a commit(), so
            # letting the exception escape this block would hit Database.session()'s own
            # rollback and undo it. Commit first so the FAILED ledger row survives for
            # the worker loop to classify.
            try:
                service.run_generation(
                    context["purchase_order_id"],
                    summary_job.as_of_date,
                    heartbeat=heartbeat,
                )
            except Exception:
                session.commit()
                raise


@dataclass
class EnqueueResult:
    """Outcome of `enqueue_daily_run`: the created job run and how many orders it enqueued."""

    job_run_id: UUID
    purchase_order_count: int
    enqueued_count: int
    no_open_orders_note: str | None = None


def enqueue_daily_run(
    job_dispatcher: JobDispatcher,
    database: Database,
    settings: Settings,
    *,
    projection_date: date | None = None,
    stacking_mode_override: str | None = None,
) -> EnqueueResult:
    """Create the daily job_run and enqueue one ORDER_RUN item per OPEN purchase order.

    Each item's `penalty_job_item_context` row is attached in the same transaction.
    This is the nightly entry point `scripts/ops/run_daily_batch.py` calls with no
    server running, so it deliberately duplicates the HTTP batch route's enqueue logic
    rather than sharing it.
    """
    tz = ZoneInfo(settings.summary.business_timezone)
    resolved_date = projection_date or datetime.now(tz).date()

    with database.session() as session:
        job_queue = JobQueueRepository(session)
        purchase_orders = PurchaseOrderRepository(session)
        job_run_context = PenaltyJobRunContextRepository(session)
        job_item_context = PenaltyJobItemContextRepository(session)

        open_purchase_orders = purchase_orders.list_purchase_orders(order_status="OPEN")

        no_open_orders_note: str | None = None
        if not open_purchase_orders:
            no_open_orders_note = describe_no_open_orders(purchase_orders.count_by_status())
            if no_open_orders_note:
                logger.warning(no_open_orders_note)

        run = job_queue.create_run(
            job_type=JobTaskType.ORDER_RUN,
            trigger_type=JobRunType.SCHEDULED_DAILY,
            requested_item_count=len(open_purchase_orders),
        )
        job_run_id = run["id"]
        job_run_context.create(
            job_run_id=job_run_id,
            projection_date=resolved_date,
            stacking_mode_override=stacking_mode_override,
        )

        item_ids: list[UUID] = []
        for purchase_order in open_purchase_orders:
            dedupe_key = f"{purchase_order['id']}:{resolved_date.isoformat()}:{JobTaskType.ORDER_RUN}"
            item = job_queue.enqueue(
                job_run_id,
                item_type=JobTaskType.ORDER_RUN,
                dedupe_key=dedupe_key,
                max_attempts=settings.job_queue.max_attempts,
            )
            # A collision may return an item belonging to an earlier run, or a context
            # row already attached to it, so only attach and dispatch items created for
            # this run (mirrors the API route's own guard).
            if item is None or item["job_run_id"] != job_run_id:
                continue
            if job_item_context.get(item["id"]) is None:
                job_item_context.create(
                    job_item_id=item["id"],
                    purchase_order_id=purchase_order["id"],
                    projection_date=resolved_date,
                    task_type=JobTaskType.ORDER_RUN,
                    stacking_mode_override=stacking_mode_override,
                )
            item_ids.append(item["id"])

        if len(item_ids) != len(open_purchase_orders):
            logger.info(
                "Enqueued %d of %d OPEN purchase order(s); %d already in flight",
                len(item_ids),
                len(open_purchase_orders),
                len(open_purchase_orders) - len(item_ids),
            )
            job_queue.set_requested_item_count(job_run_id, len(item_ids))

    # Commit before dispatch so workers can safely claim the items.
    for item_id in item_ids:
        job_dispatcher.dispatch(item_id)

    return EnqueueResult(
        job_run_id=job_run_id,
        purchase_order_count=len(open_purchase_orders),
        enqueued_count=len(item_ids),
        no_open_orders_note=no_open_orders_note,
    )


def sweep_stranded_pending_projection_summaries(
    job_dispatcher: JobDispatcher,
    database: Database,
    settings: Settings,
    *,
    today: date | None = None,
) -> SweepResult:
    """Recover PENDING PROJECTION `penalty_summary` rows with no corresponding job item.

    Dispatches only after the DB transaction commits.
    """
    tz = ZoneInfo(settings.summary.business_timezone)
    resolved_today = today or datetime.now(tz).date()
    earliest_as_of_date = resolved_today - timedelta(days=settings.summary.pending_sweep_days)

    with database.session() as session:
        summaries = PenaltySummaryRepository(session)
        stranded = summaries.find_stranded_pending(
            earliest_as_of_date,
            resolved_today,
            SummaryType.PROJECTION,
        )

        if not stranded:
            return SweepResult(recovered_count=0)

        job_queue = JobQueueRepository(session)
        job_item_context = PenaltyJobItemContextRepository(session)

        run = job_queue.create_run(
            job_type=JobTaskType.PROJECTION_SUMMARY_REGEN,
            trigger_type=JobRunType.MANUAL_BATCH,
            requested_item_count=len(stranded),
        )
        job_run_id = run["id"]

        item_ids: list[UUID] = []
        for row in stranded:
            dedupe_key = (
                f"{row['purchase_order_id']}:{row['as_of_date'].isoformat()}:"
                f"{JobTaskType.PROJECTION_SUMMARY_REGEN}"
            )
            item = job_queue.enqueue(
                job_run_id,
                item_type=JobTaskType.PROJECTION_SUMMARY_REGEN,
                dedupe_key=dedupe_key,
                max_attempts=settings.job_queue.max_attempts,
            )
            if item is None or item["job_run_id"] != job_run_id:
                continue
            if job_item_context.get(item["id"]) is None:
                job_item_context.create(
                    job_item_id=item["id"],
                    purchase_order_id=row["purchase_order_id"],
                    projection_date=row["as_of_date"],
                    task_type=JobTaskType.PROJECTION_SUMMARY_REGEN,
                )
            item_ids.append(item["id"])

        if len(item_ids) != len(stranded):
            job_queue.set_requested_item_count(job_run_id, len(item_ids))

    for item_id in item_ids:
        job_dispatcher.dispatch(item_id)

    logger.info(
        "Recovery sweep: recovered %d stranded projection-summary row(s) under job_run_id=%s",
        len(item_ids),
        job_run_id,
    )

    return SweepResult(
        recovered_count=len(item_ids),
        job_run_id=job_run_id,
    )


def sweep_expired_po_delivery_change_requests(
    database: Database,
    *,
    as_of: datetime | None = None,
) -> SweepResult:
    """Expire PENDING PO delivery-change requests whose `expires_at` has passed.

    Each moves to EXPIRED and re-triggers projection for its purchase order. No
    JobDispatcher is involved: neither step needs an LLM call, so both run inline
    before the commit.
    """
    with database.session() as session:
        master_data = MasterDataRepository(session)
        service = PoDeliveryChangeRequestService(
            purchase_orders=PurchaseOrderRepository(session),
            delivery_change_requests=PoDeliveryChangeRequestRepository(session),
            projection_service=_build_projection_service(session),
            master_data=master_data,
        )
        expired = service.expire_stale(as_of)

    if expired:
        logger.info(
            "Recovery sweep: expired %d stale PO delivery-change request(s).",
            len(expired),
        )

    return SweepResult(recovered_count=len(expired))
