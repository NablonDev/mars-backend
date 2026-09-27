"""API schemas for `POST /api/v1/cmir/email-events` (was `POST /ingest/emails`)
and the internal Service Bus consumer's `POST /internal/process-email`.

Was the "API request/response DTOs" half of the flat `app/schemas/cmir.py`.
Class names drop the stale `Ingest*Emails*` wording in favor of the new
route's noun (`email-events`), per the approved plan §6's "Rename DTO
classes dropping stale prefixes where the rest of the rename already
applies elsewhere." `batch_id` stays the wire field name throughout --
it is a stringified `process.job_run.id` (see
`CmirRunService.start_email_ingest`), but renaming the wire vocabulary
itself is a service-layer change out of scope for this API-surface phase.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class IngestFilters(BaseModel):
    subject_contains: str | None = None
    unread_only: bool = True


class IngestEmailEventsRequest(BaseModel):
    max_workers: int = Field(default=4, ge=1)
    source: str = "gmail"
    filters: IngestFilters = Field(default_factory=IngestFilters)


class EmailIngestThreadSummary(BaseModel):
    """One queued-email row from `start_email_ingest` -- pre-thread-creation,
    so `agent_run_id`/`thread_id`/`pending_action_id` are always `None` here
    (a `process.workflow_thread` only exists once a run's first human
    interrupt fires -- see `CmirRunService`'s module docstring)."""

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
    # Always `None` from `start_email_ingest` today (no thread exists yet at
    # this pre-interrupt stage), but typed `datetime` for consistency with
    # every other `updated_at`/timestamp field on a real DB row.
    updated_at: datetime | None = None


class IngestEmailEventsResponse(BaseModel):
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
    """Body for the internal `POST /internal/process-email` route -- called
    by the Service Bus consumer, not a PRD-facing route, so it keeps its
    existing (non-plural-noun) path; see `app/api/v1/cmir.py`."""

    batch_id: str
    email_id: str
    queue_message_id: str
    email: dict[str, Any]
