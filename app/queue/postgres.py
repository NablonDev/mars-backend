"""Postgres-backed queue implementation.

Job state remains durable in `job_item`; this backend uses database polling
for both dispatch and work discovery.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import update

from app.db.session import Database
from app.models import JobItem
from app.queue.interfaces import JobDispatcher, JobSource
from app.queue.types import ClaimedJob, claimed_job_from_row
from app.repositories.process.job_queue import JobQueueRepository
from app.utils.clock import utc_now_naive

logger = logging.getLogger(__name__)


class PostgresJobQueue(JobDispatcher, JobSource):
    """Queue implementation backed entirely by Postgres.

    Each operation uses a short-lived database session. Sessions are never
    held while application work, including LLM calls, is executing.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    def dispatch(self, job_item_id: UUID, *, delay_seconds: int = 0) -> None:
        """Record dispatch time; polling discovers the job independently."""
        with self._database.session() as session:
            session.execute(
                update(JobItem).where(JobItem.id == job_item_id).values(dispatched_at=utc_now_naive())
            )

    def claim_batch(self, worker_id: str, limit: int) -> list[ClaimedJob]:
        """Atomically claim up to `limit` PENDING/available job items for this worker."""
        with self._database.session() as session:
            rows = JobQueueRepository(session).claim_batch(worker_id, limit)
            return [claimed_job_from_row(row) for row in rows]

    def heartbeat(self, job: ClaimedJob, worker_id: str) -> bool:
        """Renew this worker's claim; return False if ownership was lost (e.g. reclaimed as stale)."""
        with self._database.session() as session:
            return JobQueueRepository(session).heartbeat(job.job_item_id, worker_id)

    def ack(self, job: ClaimedJob, worker_id: str) -> None:
        """Mark the job succeeded; a no-op logged as a warning if this worker no longer owns it."""
        with self._database.session() as session:
            result = JobQueueRepository(session).mark_succeeded(job.job_item_id, worker_id)
        if result is None:
            logger.warning(
                "ack no-op: job_item_id=%s no longer owned by worker_id=%s",
                job.job_item_id,
                worker_id,
            )

    def nack(
        self,
        job: ClaimedJob,
        worker_id: str,
        *,
        error: str,
        error_code: str,
        retry_in_seconds: int,
    ) -> None:
        """Record a retryable failure and make the item available again after `retry_in_seconds`."""
        with self._database.session() as session:
            result = JobQueueRepository(session).mark_failed(
                job.job_item_id, worker_id, error, error_code, retry_in_seconds
            )
        if result is None:
            logger.warning(
                "nack no-op: job_item_id=%s no longer owned by worker_id=%s", job.job_item_id, worker_id
            )

    def dead_letter(self, job: ClaimedJob, worker_id: str, *, error: str, error_code: str) -> None:
        """Mark the job DEAD; this backend has no separate DLQ, so the row itself is the record."""
        with self._database.session() as session:
            result = JobQueueRepository(session).mark_dead(job.job_item_id, worker_id, error, error_code)
        if result is None:
            logger.warning(
                "dead_letter no-op: job_item_id=%s no longer owned by worker_id=%s",
                job.job_item_id,
                worker_id,
            )

    def release(self, job: ClaimedJob, worker_id: str) -> None:
        """Return a claimed job to PENDING without consuming an attempt, e.g. on worker shutdown."""
        with self._database.session() as session:
            result = JobQueueRepository(session).release(job.job_item_id, worker_id)
        if result is None:
            logger.warning(
                "release no-op: job_item_id=%s no longer owned by worker_id=%s",
                job.job_item_id,
                worker_id,
            )

    def reclaim_stale(self, visibility_timeout_seconds: int) -> int:
        """Reset RUNNING items with no heartbeat in `visibility_timeout_seconds` back to PENDING."""
        with self._database.session() as session:
            return JobQueueRepository(session).reclaim_stale(visibility_timeout_seconds)

    def close(self) -> None:
        """No-op: this backend holds no resources, but callers need no backend knowledge."""
