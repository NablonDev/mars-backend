"""Combined-run job execution for `JobTaskType.PENALTY_FULL_RUN`.

One `process.job_item` per matching purchase order carries the requested `steps` (any
subset of `"projection"`, `"projection_summary"`, `"mitigation"`,
`"mitigation_summary"`) on `process.job_item.metadata`. `run_full_run` adds no domain
logic: it reads `steps` back and sequences the four existing step functions in fixed
dependency order, whatever order they were submitted in.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.db.session import Database
from app.queue.types import ClaimedJob
from app.repositories.process.job_queue import JobQueueRepository
from app.workers.penalty_mitigation import run_mitigation, run_mitigation_summary
from app.workers.penalty_projection import run_projection, run_summary


def _missing_steps_error(job_item_id: UUID) -> ValueError:
    """Build the error for a job item with no `steps` recorded.

    A bare `ValueError` is what `classify_failure` dead-letters, and this can never
    succeed on retry.
    """
    return ValueError(
        f"No `steps` found in process.job_item.metadata for job_item_id={job_item_id!r}; "
        "cannot execute PENALTY_FULL_RUN without knowing which steps to run."
    )


def run_full_run(
    job: ClaimedJob,
    database: Database,
    llm: AzureOpenAIChatClient,
    *,
    heartbeat: Callable[[], None] | None,
) -> None:
    """Execute the requested `steps` in fixed dependency order.

    The order is projection, projection_summary, mitigation, mitigation_summary. Each
    step function opens and commits its own session; this is only the sequencing layer.
    """
    with database.session() as session:
        item = JobQueueRepository(session).get_item(job.job_item_id)

    if item is None:
        raise _missing_steps_error(job.job_item_id)

    steps: set[str] = set(item["metadata_json"].get("steps") or [])
    if not steps:
        raise _missing_steps_error(job.job_item_id)

    if "projection" in steps:
        run_projection(job, database)
    if "projection_summary" in steps:
        run_summary(job, database, llm, heartbeat=heartbeat)
    if "mitigation" in steps:
        run_mitigation(job, database)
    if "mitigation_summary" in steps:
        run_mitigation_summary(job, database, llm, heartbeat=heartbeat)
