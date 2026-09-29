"""Dispute-summary job execution, shaped like `app.workers.penalty_projection.run_summary`.

The job item is addressed by a `dispute_id` read from `process.job_item.metadata_json`,
because `penalty_job_item_context` carries no dispute-specific column; that table is
still consulted for `force_regenerate_summary`. Only the LLM narrative is ever queued:
the deterministic `analyze()` step stays synchronous through the API.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.db.session import Database
from app.models.enums import SummaryStatus
from app.queue.types import ClaimedJob
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.dispute import PenaltyDisputeRepository
from app.repositories.penalties.job_context import PenaltyJobItemContextRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.penalties.dispute.summary_service import DisputeSummaryService


def _missing_metadata_error(job_item_id) -> ValueError:
    """Build the non-retryable error for a job item with no dispute_id recorded."""
    return ValueError(
        f"No dispute_id found on process.job_item.metadata for job_item_id={job_item_id!r}; "
        "cannot execute this DISPUTE_SUMMARY_REGEN job."
    )


def run_dispute_summary(
    job: ClaimedJob,
    database: Database,
    llm: AzureOpenAIChatClient,
    *,
    heartbeat: Callable[[], None] | None,
) -> None:
    """Regenerate the LLM narrative over an already-analyzed dispute.

    `run_generation` only flushes its FAILED row, so a failure commits explicitly
    before re-raising; otherwise `Database.session()` would roll that row back before
    the worker loop could classify the failure.
    """
    with database.session() as session:
        job_item = JobQueueRepository(session).get_item(job.job_item_id)
        raw_dispute_id = job_item["metadata_json"].get("dispute_id") if job_item is not None else None
        if raw_dispute_id is None:
            raise _missing_metadata_error(job.job_item_id)
        dispute_id = UUID(str(raw_dispute_id))

        context = PenaltyJobItemContextRepository(session).get(job.job_item_id)
        force_regenerate = bool(context["force_regenerate_summary"]) if context is not None else False

        service = DisputeSummaryService(
            purchase_orders=PurchaseOrderRepository(session),
            summaries=PenaltySummaryRepository(session),
            agent_registry=AgentRegistryRepository(session),
            job_queue=JobQueueRepository(session),
            job_context=PenaltyJobItemContextRepository(session),
            llm=llm,
            disputes=PenaltyDisputeRepository(session),
            rules=PenaltyRuleRepository(session),
            master_data=MasterDataRepository(session),
        )
        summary_job = service.get_or_schedule_for_dispute(dispute_id, force_regenerate=force_regenerate)
        if summary_job.status == SummaryStatus.PENDING:
            # Same explicit-commit-before-reraise reasoning as
            # `app.workers.penalty_projection.run_summary`.
            try:
                service.run_generation(
                    summary_job.purchase_order_id, summary_job.as_of_date, heartbeat=heartbeat
                )
            except Exception:
                session.commit()
                raise
