"""Recovery path for `PO_VALIDATION` job items left PENDING/RUNNING.

`PoValidationService.ingest_po_lines` normally claims and settles each item inline in
the same request, so only a process death between enqueue and settle can strand one.

Graph execution stays inside the API process, not `app.workers.dispatch`'s generic
claim/execute loop, which carries no LangGraph dependency. Recovery is therefore scoped
to one `job_run_id` at a time through `claim_batch`'s `job_item_ids` filter: that method
has no `item_type` filter, so a blind claim could steal another domain's PENDING item
from the generic worker loop that owns it.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.models.enums import JobItemStatus, JobTaskType
from app.repositories.cmir.job_context import CmirJobItemContextRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.po_validation.service import PoValidationService

logger = logging.getLogger(__name__)


def replay_stranded_job_run_items(
    service: PoValidationService,
    job_queue: JobQueueRepository,
    job_item_context: CmirJobItemContextRepository,
    job_run_id: UUID,
    *,
    worker_id: str,
    visibility_timeout_seconds: int,
) -> int:
    """Reclaim and re-run every stranded `PO_VALIDATION` item under one ingest batch.

    Returns the number of items recovered.
    """
    job_queue.reclaim_stale(visibility_timeout_seconds)

    pending_ids = [
        row["id"]
        for row in job_queue.list_run_items(job_run_id, status=JobItemStatus.PENDING)
        if row["item_type"] == JobTaskType.PO_VALIDATION
    ]
    if not pending_ids:
        return 0

    claimed_ids = {job["id"] for job in job_queue.claim_batch(worker_id, len(pending_ids), pending_ids)}
    recovered = 0
    for job_item_id in pending_ids:
        if job_item_id not in claimed_ids:
            continue

        context = job_item_context.get(job_item_id)
        purchase_order_line_id = (context or {}).get("purchase_order_line_id")
        if purchase_order_line_id is None:
            job_queue.mark_dead(
                job_item_id,
                worker_id,
                error="No cmir_job_item_context row with a purchase_order_line_id.",
                error_code="MISSING_CONTEXT",
            )
            continue

        try:
            service.replay_line(purchase_order_line_id)
        except Exception as exc:
            logger.exception("Failed to replay PO_VALIDATION job_item_id=%s", job_item_id)
            job_queue.mark_dead(job_item_id, worker_id, error=str(exc), error_code=type(exc).__name__)
            continue

        job_queue.mark_succeeded(job_item_id, worker_id)
        recovered += 1

    return recovered
