"""Queue abstraction for durable job state and asynchronous dispatch.

This package holds two unrelated systems that happen to share a folder:

1. The generic durable job queue, reached via `build_job_queue` and backed by
   `postgres.py` and/or `service_bus.py`. Postgres `job_item` rows are the source of
   truth for work state whatever the dispatch backend; the backend only decides how
   workers learn that work is available. Callers depend on the `JobDispatcher` and
   `JobSource` protocols alone.
2. `cmir_mail_producer.py`, the cmir-only mail notification channel
   (`ServiceBusMailQueue`) used solely by `Container`. It shares no code or interface
   with the job-queue system above.
"""

from app.queue.factory import build_job_queue
from app.queue.interfaces import JobDispatcher, JobSource
from app.queue.types import ClaimedJob

__all__ = ["ClaimedJob", "JobDispatcher", "JobSource", "build_job_queue"]
