"""Repository for cmir.email_event."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import EmailEvent
from app.utils.clock import utc_now


def _to_dict(row: EmailEvent) -> dict:
    """Project an `EmailEvent` row onto the plain dict shape returned to callers, including queue state."""
    return {
        "id": row.id,
        "sender": row.sender,
        "subject": row.subject,
        "raw_content": row.raw_content,
        "source_message_id": row.source_message_id,
        "source_imap_id": row.source_imap_id,
        "extracted_json": row.extracted_json,
        "missing_fields": row.missing_fields,
        "status": row.status,
        "queue_status": row.queue_status,
        "queue_message_id": row.queue_message_id,
        "queue_error": row.queue_error,
        "queued_at": row.queued_at,
        "processing_started_at": row.processing_started_at,
        "processed_at": row.processed_at,
        "queue_delivery_count": row.queue_delivery_count,
        "created_at": row.created_at,
    }


class EmailRepository:
    """CMIR inbound email events.

    Covers both the queue-driven ingestion path (`save_for_queue`,
    `claim_new_for_queue`, `mark_*`) and the direct, already-processed path
    (`save`).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(
        self,
        sender: str,
        subject: str | None,
        raw_content: str | None,
        source_message_id: str | None = None,
        source_imap_id: str | None = None,
    ) -> UUID:
        """Insert an email event already marked processed, bypassing the queue lifecycle."""
        row = EmailEvent(
            sender=sender,
            subject=subject,
            raw_content=raw_content,
            source_message_id=source_message_id,
            source_imap_id=source_imap_id,
            queue_status="processed",
            processed_at=utc_now(),
        )
        self._session.add(row)
        self._session.flush()
        return row.id

    def save_for_queue(
        self,
        sender: str,
        subject: str | None,
        raw_content: str | None,
        source_message_id: str | None = None,
        source_imap_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the existing row for this message/IMAP id, or insert one as `new`.

        Dedupe matches `source_message_id` or `source_imap_id`, since a row may have
        been created from either source, so repeated calls for the same inbound email
        are safe.
        """
        existing = None
        filters = []
        if source_message_id:
            filters.append(EmailEvent.source_message_id == source_message_id)
        if source_imap_id:
            filters.append(EmailEvent.source_imap_id == source_imap_id)
        if filters:
            existing = self._session.scalars(select(EmailEvent).where(or_(*filters)).limit(1)).first()

        if existing is None:
            existing = EmailEvent(
                sender=sender,
                subject=subject,
                raw_content=raw_content,
                source_message_id=source_message_id,
                source_imap_id=source_imap_id,
                queue_status="new",
            )
            self._session.add(existing)
            self._session.flush()

        return _to_dict(existing)

    def claim_new_for_queue(self, limit: int) -> list[dict[str, Any]]:
        """Claim up to `limit` `new` emails, oldest first, for one enqueuer to publish.

        `SELECT ... FOR UPDATE SKIP LOCKED` gives concurrent enqueuers disjoint
        batches instead of racing on the same rows. Claimed rows move to
        `enqueueing` before publishing is attempted, so a crash in between leaves the
        row visibly stuck rather than silently reprocessed as `new`; recovery is a
        monitoring concern, not handled here.
        """
        rows = self._session.scalars(
            select(EmailEvent)
            .where(EmailEvent.queue_status == "new")
            .order_by(EmailEvent.created_at.asc(), EmailEvent.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        if not rows:
            return []

        ids = [row.id for row in rows]
        self._session.execute(
            update(EmailEvent)
            .where(EmailEvent.id.in_(ids))
            .values(queue_status="enqueueing", queue_error=None, updated_at=func.now())
        )
        self._session.flush()
        return [_to_dict(row) for row in rows]

    def mark_queued(self, email_id: UUID, queue_message_id: str) -> None:
        """Record that the email was successfully published to the queue."""
        self._session.execute(
            update(EmailEvent)
            .where(EmailEvent.id == email_id)
            .values(
                queue_status="queued",
                queue_message_id=queue_message_id,
                queued_at=utc_now(),
                queue_error=None,
                updated_at=func.now(),
            )
        )
        self._session.flush()

    def mark_processing(self, email_id: UUID, queue_message_id: str | None = None) -> None:
        """Mark the email as being worked and bump its delivery count.

        The delivery count increments on every processing attempt, not just
        retries, so it doubles as a redelivery counter for detecting a
        message stuck bouncing between queue and worker.
        """
        values: dict[str, Any] = {
            "queue_status": "processing",
            "processing_started_at": utc_now(),
            "queue_delivery_count": EmailEvent.queue_delivery_count + 1,
            "queue_error": None,
            "updated_at": func.now(),
        }
        if queue_message_id is not None:
            values["queue_message_id"] = queue_message_id

        self._session.execute(update(EmailEvent).where(EmailEvent.id == email_id).values(**values))
        self._session.flush()

    def mark_queue_processed(self, email_id: UUID) -> None:
        """Mark the email as fully processed and clear any prior queue error."""
        self._session.execute(
            update(EmailEvent)
            .where(EmailEvent.id == email_id)
            .values(
                queue_status="processed",
                processed_at=utc_now(),
                queue_error=None,
                updated_at=func.now(),
            )
        )
        self._session.flush()

    def mark_queue_failed(self, email_id: UUID, error: str, *, retryable: bool = True) -> None:
        """Record a queue-processing failure, returning the email to `new` or parking it as `failed`."""
        self._session.execute(
            update(EmailEvent)
            .where(EmailEvent.id == email_id)
            .values(queue_status="new" if retryable else "failed", queue_error=error, updated_at=func.now())
        )
        self._session.flush()

    def list_pending(self, limit: int = 50) -> list[dict[str, Any]]:
        """Rows not yet fully processed -- for a UI "email queue" panel that
        lets a reviewer manually process/"unqueue" one instead of waiting on
        the real Service Bus consumer. Covers every pre-`processed`/`failed`
        state (`new`, `enqueueing`, `queued`, `processing`), oldest first."""
        rows = self._session.scalars(
            select(EmailEvent)
            .where(EmailEvent.queue_status.in_(["new", "enqueueing", "queued", "processing"]))
            .order_by(EmailEvent.created_at.asc())
            .limit(limit)
        ).all()
        return [_to_dict(row) for row in rows]

    def get_queue_state(self, email_id: UUID) -> dict[str, Any] | None:
        """Return the queue-status projection used for polling, or None if the email is unknown."""
        row = self._session.get(EmailEvent, email_id)
        if row is None:
            return None
        return {
            "email_id": str(row.id),
            "queue_status": row.queue_status,
            "queue_message_id": row.queue_message_id,
            "queue_delivery_count": row.queue_delivery_count,
            "queue_error": row.queue_error,
            "processed_at": row.processed_at,
        }

    def update_extraction(
        self,
        email_id: UUID,
        extracted_json: dict[str, Any],
        missing_fields: list[str] | None,
        status: str | None,
    ) -> None:
        """Persist the LLM extraction result for an email onto its row."""
        self._session.execute(
            update(EmailEvent)
            .where(EmailEvent.id == email_id)
            .values(
                extracted_json=extracted_json,
                missing_fields=missing_fields,
                status=status,
                updated_at=func.now(),
            )
        )
        self._session.flush()

    def get(self, email_id: UUID) -> dict[str, Any] | None:
        """Return the full email event dict, or None if it doesn't exist."""
        row = self._session.get(EmailEvent, email_id)
        return _to_dict(row) if row is not None else None
