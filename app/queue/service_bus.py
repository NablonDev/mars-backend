"""Azure Service Bus job-queue backend.

`job_item` in Postgres remains the source of truth. Service Bus only
dispatches notifications and carries delivery state.

Azure SDK imports are deferred so the Postgres backend does not require
Service Bus dependencies at import time.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.session import Database
from app.models.enums import JobItemStatus
from app.queue.interfaces import JobDispatcher, JobSource
from app.queue.types import ClaimedJob, claimed_job_from_row
from app.repositories.process.job_queue import JobQueueRepository
from app.utils.clock import utc_now

logger = logging.getLogger(__name__)

# Keep the message until durable job state is settled.
_RECEIVE_MODE_PEEK_LOCK = "peeklock"


def _message_body_str(message: Any) -> str:
    """Return the message body as text for SDK and test messages.

    The real SDK exposes `body` as a generator of byte chunks (it streams
    large messages), while test doubles typically pass a plain `str` or
    `bytes`; all three shapes are normalized to one decoded string here.
    """
    body = message.body
    if isinstance(body, str):
        return body
    if isinstance(body, bytes):
        return body.decode("utf-8")
    return b"".join(bytes(chunk) for chunk in body).decode("utf-8")


class ServiceBusJobQueue(JobDispatcher, JobSource):
    """Service Bus dispatcher and source backed by the Postgres job ledger."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        client: Any | None = None,
        message_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._database = database
        self._queue_name = settings.job_queue.service_bus_queue_name
        self._max_wait_seconds = settings.job_queue.service_bus_max_wait_seconds

        if client is None or message_factory is None:
            client, message_factory = self._build_real_client(settings)

        self._client = client
        self._message_factory = message_factory
        self._sender = client.get_queue_sender(queue_name=self._queue_name)
        self._receiver = client.get_queue_receiver(
            queue_name=self._queue_name,
            max_wait_time=self._max_wait_seconds,
            receive_mode=_RECEIVE_MODE_PEEK_LOCK,
        )
        self._closed = False

        # The Service Bus SDK objects are shared by worker threads.
        self._lock = threading.RLock()

    @staticmethod
    def _build_real_client(settings: Settings) -> tuple[Any, Callable[[str], Any]]:
        """Build the authenticated Service Bus client.

        Always authenticates via `DefaultAzureCredential` (managed identity) --
        unlike the CMIR mail producer, this backend has no connection-string
        fallback. Raises `ValidationError` if the namespace is unconfigured or
        the Azure SDK packages are not importable, so a misconfigured
        `service_bus` backend fails fast at construction rather than at the
        first dispatch.
        """
        if not settings.job_queue.service_bus_namespace:
            raise ValidationError(
                code="SERVICE_BUS_NAMESPACE_NOT_CONFIGURED",
                message=(
                    "JOB_QUEUE_BACKEND=service_bus requires JOB_QUEUE_SERVICE_BUS_NAMESPACE "
                    "to be set (e.g. 'mars-fines.servicebus.windows.net')."
                ),
            )

        try:
            from azure.identity import DefaultAzureCredential
            from azure.servicebus import ServiceBusClient, ServiceBusMessage
        except ImportError as exc:
            raise ValidationError(
                code="SERVICE_BUS_SDK_NOT_INSTALLED",
                message=(
                    "JOB_QUEUE_BACKEND=service_bus requires the 'azure-servicebus' and "
                    "'azure-identity' packages, which are declared in "
                    "pyproject.toml/requirements.txt but are not importable in this "
                    "environment. Reinstall dependencies (e.g. `uv sync`) before "
                    "selecting this backend."
                ),
            ) from exc

        client = ServiceBusClient(
            fully_qualified_namespace=settings.job_queue.service_bus_namespace,
            credential=DefaultAzureCredential(),
        )
        return client, ServiceBusMessage

    # ------------------------------------------------------------------
    # JobDispatcher
    # ------------------------------------------------------------------

    def dispatch(self, job_item_id: UUID, *, delay_seconds: int = 0) -> None:
        """Dispatch a notification for a durable job item.

        The message carries only the job item id as its body; `job_item` in
        Postgres remains the sole source of durable state. With
        `delay_seconds > 0` the message is scheduled rather than sent
        immediately, which is how nack's retry backoff is implemented on
        this backend (Postgres has no poller for `available_at` here).
        """
        message = self._message_factory(str(job_item_id))

        if delay_seconds > 0:
            scheduled_time_utc = utc_now() + timedelta(seconds=delay_seconds)
            with self._lock:
                self._sender.schedule_messages(message, scheduled_time_utc)
        else:
            with self._lock:
                self._sender.send_messages(message)

    # ------------------------------------------------------------------
    # JobSource
    # ------------------------------------------------------------------

    def claim_batch(self, worker_id: str, limit: int) -> list[ClaimedJob]:
        """Receive messages and atomically claim their job items in Postgres."""
        with self._lock:
            messages = list(self._receiver.receive_messages(max_message_count=limit))

        if not messages:
            return []

        messages_by_id: dict[UUID, list[Any]] = {}

        for message in messages:
            try:
                job_item_id = UUID(_message_body_str(message))
            except (ValueError, TypeError):
                with self._lock:
                    self._receiver.dead_letter_message(
                        message,
                        reason="MALFORMED_BODY",
                        error_description="message body is not a UUID",
                    )
                continue

            messages_by_id.setdefault(job_item_id, []).append(message)

        if not messages_by_id:
            return []

        with self._database.session() as session:
            claimed_rows = JobQueueRepository(session).claim_batch(
                worker_id,
                limit,
                job_item_ids=list(messages_by_id),
            )

        claimed_jobs: list[ClaimedJob] = []

        for row in claimed_rows:
            job_item_id = row["id"]
            primary, *duplicates = messages_by_id.pop(job_item_id)

            claimed_jobs.append(claimed_job_from_row(row, receipt=primary))

            # Duplicate notifications cannot create a second DB claim.
            for duplicate in duplicates:
                with self._lock:
                    self._receiver.complete_message(duplicate)

        # Messages for items already claimed or terminal are no longer useful.
        for leftover_messages in messages_by_id.values():
            for message in leftover_messages:
                with self._lock:
                    self._receiver.complete_message(message)

        return claimed_jobs

    def heartbeat(self, job: ClaimedJob, worker_id: str) -> bool:
        """Renew transport and durable ownership.

        Renews the Service Bus message lock first; if that fails, the
        transport no longer guarantees exclusive delivery, so ownership is
        treated as lost without touching the DB. Only on a successful lock
        renewal does this also renew the `job_item` row's heartbeat, whose
        own result (False if another worker has since reclaimed it) is
        returned as the final answer.
        """
        try:
            with self._lock:
                self._receiver.renew_message_lock(job.receipt)
        except Exception:  # noqa: BLE001
            # If the transport lock cannot be renewed, ownership is no longer
            # safe to assume.
            logger.warning(
                "service bus lock renewal failed for job_item_id=%s worker_id=%s",
                job.job_item_id,
                worker_id,
            )
            return False

        with self._database.session() as session:
            return JobQueueRepository(session).heartbeat(
                job.job_item_id,
                worker_id,
            )

    def ack(self, job: ClaimedJob, worker_id: str) -> None:
        """Mark the job succeeded, then complete its Service Bus message."""
        # DB first prevents a crash from turning completed work into a retry.
        with self._database.session() as session:
            result = JobQueueRepository(session).mark_succeeded(
                job.job_item_id,
                worker_id,
            )

        if result is None:
            logger.warning(
                "ack no-op: job_item_id=%s no longer owned by worker_id=%s",
                job.job_item_id,
                worker_id,
            )

        with self._lock:
            self._receiver.complete_message(job.receipt)

    def nack(
        self,
        job: ClaimedJob,
        worker_id: str,
        *,
        error: str,
        error_code: str,
        retry_in_seconds: int,
    ) -> None:
        """Record a retryable failure and schedule the next attempt.

        The Service Bus message is always completed here whatever the DB outcome,
        because this backend re-drives retries through a freshly scheduled `dispatch`
        rather than message redelivery. That re-dispatch happens only while
        `mark_failed` still reports the item PENDING: a lost claim or exhausted
        attempts schedule no new message.
        """
        with self._database.session() as session:
            result = JobQueueRepository(session).mark_failed(
                job.job_item_id,
                worker_id,
                error,
                error_code,
                retry_in_seconds,
            )

        if result is None:
            logger.warning(
                "nack no-op: job_item_id=%s no longer owned by worker_id=%s",
                job.job_item_id,
                worker_id,
            )
            with self._lock:
                self._receiver.complete_message(job.receipt)
            return

        with self._lock:
            self._receiver.complete_message(job.receipt)

        if result["status"] == JobItemStatus.PENDING:
            # Service Bus has no poller for available_at; schedule the retry.
            self.dispatch(
                job.job_item_id,
                delay_seconds=retry_in_seconds,
            )

    def dead_letter(
        self,
        job: ClaimedJob,
        worker_id: str,
        *,
        error: str,
        error_code: str,
    ) -> None:
        """Mark the job DEAD and move its message to the native DLQ.

        DB state is settled first so a crash between the two steps leaves
        the job correctly DEAD even if the message never reaches the DLQ.
        `error_code`/`error` are passed through as the DLQ reason/description,
        so the native dead-letter queue carries the same failure detail as
        the `job_item` row.
        """
        with self._database.session() as session:
            result = JobQueueRepository(session).mark_dead(
                job.job_item_id,
                worker_id,
                error,
                error_code,
            )

        if result is None:
            logger.warning(
                "dead_letter no-op: job_item_id=%s no longer owned by worker_id=%s",
                job.job_item_id,
                worker_id,
            )

        with self._lock:
            self._receiver.dead_letter_message(
                job.receipt,
                reason=error_code,
                error_description=error,
            )

    def release(self, job: ClaimedJob, worker_id: str) -> None:
        """Return a claimed job to PENDING during shutdown.

        Abandons the Service Bus message rather than completing it, so the
        broker redelivers it immediately instead of waiting out the lock
        duration; this release does not consume a retry attempt.
        """
        with self._database.session() as session:
            result = JobQueueRepository(session).release(
                job.job_item_id,
                worker_id,
            )

        if result is None:
            logger.warning(
                "release no-op: job_item_id=%s no longer owned by worker_id=%s",
                job.job_item_id,
                worker_id,
            )

        # Release is immediate and does not consume an attempt.
        with self._lock:
            self._receiver.abandon_message(job.receipt)

    def reclaim_stale(self, visibility_timeout_seconds: int) -> int:
        """Reset abandoned DB claims to PENDING; a reclaimed row needs `dispatch` again."""
        with self._database.session() as session:
            return JobQueueRepository(session).reclaim_stale(visibility_timeout_seconds)

    def close(self) -> None:
        """Close Service Bus resources; safe to call more than once.

        Guarded by `self._closed` so repeated shutdown calls are harmless.
        Each of sender/receiver/client is closed independently and a failure
        closing one does not prevent the others from being attempted.
        """
        if self._closed:
            return

        self._closed = True

        for resource in (self._sender, self._receiver, self._client):
            try:
                with self._lock:
                    resource.close()
            except Exception:
                logger.warning(
                    "Error closing Service Bus resource %r",
                    resource,
                    exc_info=True,
                )
