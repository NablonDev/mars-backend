"""Build the configured job-queue backend."""

from __future__ import annotations

from app.core.config import JobQueueBackend, Settings
from app.core.exceptions import ValidationError
from app.db.session import Database
from app.queue.interfaces import JobDispatcher, JobSource
from app.queue.postgres import PostgresJobQueue
from app.queue.service_bus import ServiceBusJobQueue


def build_job_queue(
    settings: Settings,
    database: Database,
) -> tuple[JobDispatcher, JobSource]:
    """Build the dispatcher and source for the configured backend.

    Both capabilities are returned separately so callers depend only on the
    protocol required by their operation.
    """
    backend = settings.job_queue.backend

    if backend == JobQueueBackend.POSTGRES:
        queue = PostgresJobQueue(database)
        return queue, queue

    if backend == JobQueueBackend.SERVICE_BUS:
        service_bus_queue = ServiceBusJobQueue(database, settings)
        return service_bus_queue, service_bus_queue

    raise ValidationError(
        code="UNKNOWN_JOB_QUEUE_BACKEND",
        message=f"Unknown job_queue.backend={backend}; expected one of {tuple(JobQueueBackend)}.",
    )
