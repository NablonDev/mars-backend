"""Dispatch table routing one claimed job item to its domain worker.

Nothing but routing lives here: the per-task-type work sits in the domain modules
imported below, so the penalty sub-domains' execution logic never interleaves.
"""

from __future__ import annotations

from collections.abc import Callable

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import Settings
from app.db.session import Database
from app.models.enums import JobTaskType
from app.queue.types import ClaimedJob
from app.workers.penalty_dispute import run_dispute_summary
from app.workers.penalty_full_run import run_full_run
from app.workers.penalty_mitigation import run_mitigation, run_mitigation_summary
from app.workers.penalty_projection import run_projection, run_summary


def execute_job(
    job: ClaimedJob,
    database: Database,
    # Part of the fixed handler contract (see loop.py's execute_job_fn seam): unused
    # today, kept so a future settings-driven knob needs no signature change.
    settings: Settings,
    llm: AzureOpenAIChatClient,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    """Execute one job; the worker loop classifies any exception raised here.

    Each regeneration type requires its upstream facts to be persisted already: a
    projection for PROJECTION_SUMMARY_REGEN, mitigation options for
    MITIGATION_SUMMARY_REGEN, and an ANALYZED penalty_dispute for
    DISPUTE_SUMMARY_REGEN, whose verdict only ever comes from the synchronous API.
    PENALTY_FULL_RUN runs the requested subset of the other steps in dependency order.
    """
    if job.item_type == JobTaskType.ORDER_RUN:
        run_projection(job, database)
        run_summary(job, database, llm, heartbeat=heartbeat)
    elif job.item_type == JobTaskType.PROJECTION_SUMMARY_REGEN:
        run_summary(job, database, llm, heartbeat=heartbeat)
    elif job.item_type == JobTaskType.MITIGATION_RUN:
        run_mitigation(job, database)
    elif job.item_type == JobTaskType.MITIGATION_SUMMARY_REGEN:
        run_mitigation_summary(job, database, llm, heartbeat=heartbeat)
    elif job.item_type == JobTaskType.DISPUTE_SUMMARY_REGEN:
        run_dispute_summary(job, database, llm, heartbeat=heartbeat)
    elif job.item_type == JobTaskType.PENALTY_FULL_RUN:
        run_full_run(job, database, llm, heartbeat=heartbeat)
    else:
        # Unknown task types are non-retryable.
        raise ValueError(f"Unknown item_type={job.item_type!r} for job_item_id={job.job_item_id}")
