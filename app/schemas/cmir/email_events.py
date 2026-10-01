"""API schemas for `POST /cmir/email-events` and the internal `POST /internal/process-email`.

`batch_id` is the wire name for a stringified `process.job_run.id`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class IngestFilters(BaseModel):
    """Mailbox filters applied when fetching emails for `IngestEmailEventsRequest`."""

    subject_contains: str | None = None
    unread_only: bool = True


class IngestEmailEventsRequest(BaseModel):
    """Request body for `POST /api/v1/cmir/email-events`, kicking off a CMIR email-ingest batch."""

    max_workers: int = Field(default=4, ge=1)
    source: str = "gmail"
    filters: IngestFilters = Field(default_factory=IngestFilters)


class EmailIngestThreadSummary(BaseModel):
    """One queued-email row from `start_email_ingest`, before any workflow thread exists."""

    batch_id: str | None = None
    agent_run_id: UUID | None = None
    thread_id: str | None = None
    email_id: str
    source_message_id: str | None = None
    sender: str | None = None
    subject: str | None = None
    stage: str
    status: str
    current_node: str | None = None
    pending_action_id: UUID | None = None
    # Always `None` at this pre-interrupt stage, but typed `datetime` for consistency with
    # every other timestamp field on a real DB row.
    updated_at: datetime | None = None


class IngestEmailEventsResponse(BaseModel):
    """Response shape for `POST /api/v1/cmir/email-events`: the created batch and its queued threads."""

    batch_id: str
    status: str
    total_threads: int
    threads: list[EmailIngestThreadSummary]


class PendingEmailSummary(BaseModel):
    """One row of `GET /cmir/email-events/pending` -- the UI "email queue"
    panel that lets a reviewer manually process a queued email instead of
    waiting on the real Service Bus consumer. Real `cmir.email_event`
    columns only (see `EmailRepository.list_pending`)."""

    id: UUID
    sender: str
    subject: str | None = None
    raw_content: str | None = None
    queue_status: str
    queued_at: datetime | None = None
    processing_started_at: datetime | None = None
    queue_delivery_count: int
    created_at: datetime


class ProcessQueuedEmailRequest(BaseModel):
    """Body for the internal `POST /internal/process-email` route, called by the queue consumer."""

    batch_id: str
    email_id: str
    queue_message_id: str
    email: dict[str, Any]
