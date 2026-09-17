"""API endpoints for job run batch triggers, status, and item listing."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.dependencies import (
    get_job_dispatcher,
    get_job_queue_repository,
    get_penalty_job_item_context_repository,
    get_penalty_job_run_context_repository,
    get_penalty_projection_repository,
    get_purchase_order_repository,
    get_session,
)
from app.core.config import Settings, get_settings
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import NotFoundError
from app.models import JobRun
from app.models.enums import JobItemStatus, JobRunType, JobTaskType
from app.queue.interfaces import JobDispatcher
from app.repositories.common.purchase_order import PurchaseOrderRepository, describe_no_open_orders
from app.repositories.penalties.job_context import (
    PenaltyJobItemContextRepository,
    PenaltyJobRunContextRepository,
)
from app.repositories.penalties.projection import PenaltyProjectionRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.schemas.penalties.batches import (
    JobItemListResponse,
    JobItemResponse,
    JobRunRequest,
    JobRunResponse,
    JobRunStatusCounts,
    JobRunStatusResponse,
    PenaltyFullRunBatchRequest,
    PenaltyMitigationBatchRequest,
    PenaltyProjectionBatchRequest,
)
from app.utils.clock import utc_today

logger = logging.getLogger(__name__)

router = APIRouter(tags=["job-runs"])

# Backend-derived execution notes. Do not provide an ETA or timestamp:
# pickup timing depends on the configured queue backend.
_EXECUTION_NOTES: dict[str, str] = {
    "service_bus": "Dispatched to the queue; a consumer will pick it up when available.",
    "postgres": "Enqueued; will be processed by the next scheduled batch drain.",
}


def _execution_note(job_queue_backend: str) -> str:
    """Return a human-readable message describing how jobs are processed by the configured backend."""
    return _EXECUTION_NOTES.get(
        job_queue_backend,
        f"Enqueued under job_queue_backend={job_queue_backend!r}; pickup timing depends on that backend.",
    )


@router.post(
    "/job-runs",
    response_model=Envelope[JobRunResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_job_run(
    body: JobRunRequest,
    session: Session = Depends(get_session),
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    projections: PenaltyProjectionRepository = Depends(get_penalty_projection_repository),
    job_queue: JobQueueRepository = Depends(get_job_queue_repository),
    job_run_context: PenaltyJobRunContextRepository = Depends(get_penalty_job_run_context_repository),
    job_item_context: PenaltyJobItemContextRepository = Depends(get_penalty_job_item_context_repository),
    job_dispatcher: JobDispatcher = Depends(get_job_dispatcher),
    settings: Settings = Depends(get_settings),
) -> Envelope[JobRunResponse]:
    """Enqueue a batch job run, dispatching on the `job_type` discriminator."""
    if isinstance(body, PenaltyMitigationBatchRequest):
        return _trigger_penalty_mitigation_batch(
            body,
            session,
            purchase_orders,
            projections,
            job_queue,
            job_run_context,
            job_item_context,
            job_dispatcher,
            settings,
        )
    if isinstance(body, PenaltyFullRunBatchRequest):
        return _trigger_penalty_full_run_batch(
            body,
            session,
            purchase_orders,
            projections,
            job_queue,
            job_run_context,
            job_item_context,
            job_dispatcher,
            settings,
        )
    return _trigger_penalty_projection_batch(
        body,
        session,
        purchase_orders,
        job_queue,
        job_run_context,
        job_item_context,
        job_dispatcher,
        settings,
    )


def _trigger_penalty_projection_batch(
    body: PenaltyProjectionBatchRequest,
    session: Session,
    purchase_orders: PurchaseOrderRepository,
    job_queue: JobQueueRepository,
    job_run_context: PenaltyJobRunContextRepository,
    job_item_context: PenaltyJobItemContextRepository,
    job_dispatcher: JobDispatcher,
    settings: Settings,
) -> Envelope[JobRunResponse]:
    """Enqueue a full penalty-projection batch for every OPEN purchase order.

    Only creates and dispatches jobs; a worker actually running the
    projection for each item is a separate concern.
    """
    projection_date = body.projection_date or utc_today()
    open_purchase_orders = purchase_orders.list_purchase_orders(order_status="OPEN")

    no_open_orders_note: str | None = None
    if not open_purchase_orders:
        no_open_orders_note = describe_no_open_orders(purchase_orders.count_by_status())
        if no_open_orders_note:
            logger.warning(no_open_orders_note)

    run = job_queue.create_run(
        job_type=JobTaskType.ORDER_RUN,
        trigger_type=JobRunType.MANUAL_BATCH,
        requested_item_count=len(open_purchase_orders),
    )
    job_run_context.create(
        job_run_id=run["id"],
        projection_date=projection_date,
        stacking_mode_override=body.stacking_mode_override,
    )

    item_ids: list[UUID] = []
    for purchase_order in open_purchase_orders:
        dedupe_key = f"{purchase_order['id']}:{projection_date.isoformat()}:{JobTaskType.ORDER_RUN}"
        item = job_queue.enqueue(
            run["id"],
            item_type=JobTaskType.ORDER_RUN,
            dedupe_key=dedupe_key,
            max_attempts=settings.job_queue.max_attempts,
        )
        # A collision may return an item belonging to an earlier run, or a
        # context row already attached to it, so only attach and dispatch
        # items created for this run.
        if item is None or item["job_run_id"] != run["id"]:
            continue
        if job_item_context.get(item["id"]) is None:
            job_item_context.create(
                job_item_id=item["id"],
                purchase_order_id=purchase_order["id"],
                projection_date=projection_date,
                task_type=JobTaskType.ORDER_RUN,
                stacking_mode_override=body.stacking_mode_override,
            )
        item_ids.append(item["id"])

    if len(item_ids) != len(open_purchase_orders):
        job_queue.set_requested_item_count(run["id"], len(item_ids))

    session.commit()

    for item_id in item_ids:
        job_dispatcher.dispatch(item_id)

    return success_envelope(
        JobRunResponse(
            job_run_id=run["id"],
            requested_item_count=len(item_ids),
            dispatch_mode=settings.job_queue.backend,
            # The normal note promises a drain will process this; with zero
            # items that would be actively misleading.
            execution_note=no_open_orders_note or _execution_note(settings.job_queue.backend),
        ),
        message="Job run queued.",
    )


def _trigger_penalty_mitigation_batch(
    body: PenaltyMitigationBatchRequest,
    session: Session,
    purchase_orders: PurchaseOrderRepository,
    projections: PenaltyProjectionRepository,
    job_queue: JobQueueRepository,
    job_run_context: PenaltyJobRunContextRepository,
    job_item_context: PenaltyJobItemContextRepository,
    job_dispatcher: JobDispatcher,
    settings: Settings,
) -> Envelope[JobRunResponse]:
    """Enqueue mitigation-compute batch for every OPEN PO with a penalty projection."""
    run_date = utc_today()
    open_purchase_orders = purchase_orders.list_purchase_orders(order_status="OPEN")

    no_open_orders_note: str | None = None
    if not open_purchase_orders:
        no_open_orders_note = describe_no_open_orders(purchase_orders.count_by_status())
        if no_open_orders_note:
            logger.warning(no_open_orders_note)

    run = job_queue.create_run(
        job_type=JobTaskType.MITIGATION_RUN,
        trigger_type=JobRunType.MANUAL_BATCH,
        requested_item_count=len(open_purchase_orders),
    )
    # `projection_date` here is this run's own trigger date, not a domain
    # input; each dispatched item's own penalty_job_item_context row carries
    # the purchase order's actual projection date. penalty_job_run_context is
    # write-only bookkeeping today, so this nominal value is safe.
    job_run_context.create(job_run_id=run["id"], projection_date=run_date)

    item_ids: list[UUID] = []
    for purchase_order in open_purchase_orders:
        latest_projection = projections.get_latest(purchase_order["id"])
        if latest_projection is None:
            # No projection exists yet for this PO, so it is not eligible for
            # mitigation, mirroring MitigationService.run_for_purchase_order's
            # own NO_PROJECTION_EXISTS check. Skip, don't fail the batch.
            continue

        projection_date = latest_projection["projection_date"]
        dedupe_key = f"{purchase_order['id']}:{projection_date.isoformat()}:{JobTaskType.MITIGATION_RUN}"
        item = job_queue.enqueue(
            run["id"],
            item_type=JobTaskType.MITIGATION_RUN,
            dedupe_key=dedupe_key,
            max_attempts=settings.job_queue.max_attempts,
        )
        # A collision may return an item belonging to an earlier run, or a
        # context row already attached to it, so only attach and dispatch
        # items created for this run.
        if item is None or item["job_run_id"] != run["id"]:
            continue
        if job_item_context.get(item["id"]) is None:
            job_item_context.create(
                job_item_id=item["id"],
                purchase_order_id=purchase_order["id"],
                projection_date=projection_date,
                task_type=JobTaskType.MITIGATION_RUN,
            )
        item_ids.append(item["id"])

    if len(item_ids) != len(open_purchase_orders):
        job_queue.set_requested_item_count(run["id"], len(item_ids))

    session.commit()

    for item_id in item_ids:
        job_dispatcher.dispatch(item_id)

    if no_open_orders_note:
        execution_note = no_open_orders_note
    elif open_purchase_orders and not item_ids:
        execution_note = (
            f"{len(open_purchase_orders)} OPEN purchase order(s) exist, but none have a penalty "
            "projection yet. Run job_type=PENALTY_PROJECTION_BATCH first."
        )
    else:
        execution_note = _execution_note(settings.job_queue.backend)

    return success_envelope(
        JobRunResponse(
            job_run_id=run["id"],
            requested_item_count=len(item_ids),
            dispatch_mode=settings.job_queue.backend,
            execution_note=execution_note,
        ),
        message="Job run queued.",
    )


def _trigger_penalty_full_run_batch(
    body: PenaltyFullRunBatchRequest,
    session: Session,
    purchase_orders: PurchaseOrderRepository,
    projections: PenaltyProjectionRepository,
    job_queue: JobQueueRepository,
    job_run_context: PenaltyJobRunContextRepository,
    job_item_context: PenaltyJobItemContextRepository,
    job_dispatcher: JobDispatcher,
    settings: Settings,
) -> Envelope[JobRunResponse]:
    """Enqueue a full penalty workflow run for matching purchase orders."""
    run_date = body.projection_date or utc_today()
    needs_existing_projection = "projection" not in body.steps

    if body.scope.purchase_order_ids is not None:
        matching_purchase_orders = purchase_orders.list_purchase_orders(
            purchase_order_ids=body.scope.purchase_order_ids
        )
    else:
        matching_purchase_orders = purchase_orders.list_purchase_orders(
            order_status=body.scope.purchase_order_status
        )

    no_matching_orders_note: str | None = None
    if not matching_purchase_orders:
        if body.scope.purchase_order_ids is not None:
            no_matching_orders_note = "No purchase orders matched the requested purchase_order_ids."
        else:
            no_matching_orders_note = describe_no_open_orders(purchase_orders.count_by_status())
        if no_matching_orders_note:
            logger.warning(no_matching_orders_note)

    run = job_queue.create_run(
        job_type=JobTaskType.PENALTY_FULL_RUN,
        trigger_type=JobRunType.MANUAL_BATCH,
        requested_item_count=len(matching_purchase_orders),
    )
    # `projection_date` here is this run's own trigger/override date; each
    # dispatched item's own penalty_job_item_context row carries the purchase
    # order's actual per-item projection date. See
    # _trigger_penalty_mitigation_batch's identical note.
    job_run_context.create(job_run_id=run["id"], projection_date=run_date)

    item_metadata = {"steps": body.steps, "projection_date": run_date.isoformat()}

    item_ids: list[UUID] = []
    for purchase_order in matching_purchase_orders:
        if needs_existing_projection:
            latest_projection = projections.get_latest(purchase_order["id"])
            if latest_projection is None:
                # No projection exists yet, and this run isn't producing one
                # either, so it is not eligible. Skip, don't fail the batch.
                continue
            projection_date = latest_projection["projection_date"]
        else:
            projection_date = run_date

        dedupe_key = f"{purchase_order['id']}:{projection_date.isoformat()}:{JobTaskType.PENALTY_FULL_RUN}"
        item = job_queue.enqueue(
            run["id"],
            item_type=JobTaskType.PENALTY_FULL_RUN,
            dedupe_key=dedupe_key,
            max_attempts=settings.job_queue.max_attempts,
            metadata=item_metadata,
        )
        # A collision may return an item belonging to an earlier run, or a
        # context row already attached to it, so only attach and dispatch
        # items created for this run.
        if item is None or item["job_run_id"] != run["id"]:
            continue
        if job_item_context.get(item["id"]) is None:
            job_item_context.create(
                job_item_id=item["id"],
                purchase_order_id=purchase_order["id"],
                projection_date=projection_date,
                task_type=JobTaskType.PENALTY_FULL_RUN,
            )
        item_ids.append(item["id"])

    if len(item_ids) != len(matching_purchase_orders):
        job_queue.set_requested_item_count(run["id"], len(item_ids))

    session.commit()

    for item_id in item_ids:
        job_dispatcher.dispatch(item_id)

    if no_matching_orders_note:
        execution_note = no_matching_orders_note
    elif matching_purchase_orders and not item_ids and needs_existing_projection:
        execution_note = (
            f"{len(matching_purchase_orders)} matching purchase order(s) exist, but none have a "
            'penalty projection yet. Include "projection" in steps, or run '
            "job_type=PENALTY_PROJECTION_BATCH first."
        )
    else:
        execution_note = _execution_note(settings.job_queue.backend)

    return success_envelope(
        JobRunResponse(
            job_run_id=run["id"],
            requested_item_count=len(item_ids),
            dispatch_mode=settings.job_queue.backend,
            execution_note=execution_note,
        ),
        message="Job run queued.",
    )


@router.get("/job-runs/{job_run_id}", response_model=Envelope[JobRunStatusResponse])
def get_job_run_status(
    job_run_id: UUID,
    session: Session = Depends(get_session),
    job_queue: JobQueueRepository = Depends(get_job_queue_repository),
) -> Envelope[JobRunStatusResponse]:
    """Retrieve the execution status and completion state of a job run."""
    # A run with zero items is valid; absence of the JobRun distinguishes
    # "run does not exist" from "run exists but has no items".
    if session.get(JobRun, job_run_id) is None:
        raise NotFoundError(
            code="JOB_RUN_NOT_FOUND", message=f"No job run found with job_run_id={job_run_id}"
        )

    summary = job_queue.get_run_summary(job_run_id)
    counts = summary["counts"]

    # Completion is based on persisted items, not requested_item_count. A
    # run with no persisted items has not started and cannot be complete.
    total_items = summary["total_items"]
    is_complete = (
        total_items > 0 and (counts[JobItemStatus.SUCCEEDED] + counts[JobItemStatus.DEAD]) == total_items
    )

    return success_envelope(
        JobRunStatusResponse(
            job_run_id=job_run_id,
            requested_item_count=summary["requested_item_count"],
            counts=JobRunStatusCounts(**counts),
            total_items=total_items,
            is_complete=is_complete,
        )
    )


@router.get(
    "/job-runs/{job_run_id}/items",
    response_model=Envelope[JobItemListResponse],
)
def list_job_run_items(
    job_run_id: UUID,
    status: JobItemStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    job_queue: JobQueueRepository = Depends(get_job_queue_repository),
) -> Envelope[JobItemListResponse]:
    """List items in a job run, optionally filtered by status, with pagination."""
    rows = job_queue.list_run_items(job_run_id, status=status, limit=limit, offset=offset)

    items = [
        JobItemResponse(
            id=row["id"],
            item_type=row["item_type"],
            dedupe_key=row["dedupe_key"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            last_error_code=row["last_error_code"],
            # Expose only a safe message derived from the error code. Never
            # return raw exception text; see JobItemResponse.
            last_error_message=(
                f"Job failed with error code {row['last_error_code']}" if row["last_error_code"] else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )
        for row in rows
    ]

    return success_envelope(
        JobItemListResponse(job_run_id=job_run_id, items=items, limit=limit, offset=offset)
    )
