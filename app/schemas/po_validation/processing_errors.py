"""API schemas for `GET /api/v1/processing-errors`."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ProcessingErrorItem(BaseModel):
    """One `process.processing_error` row."""

    id: UUID
    job_item_id: UUID | None = None
    agent_run_id: UUID | None = None
    purchase_order_line_id: UUID | None = None
    error_type: str
    error_code: str | None = None
    error_message: str | None = None
    node_name: str | None = None
    occurred_at: datetime | None = None
    resolved: bool = False
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class ProcessingErrorsListResponse(BaseModel):
    """Response shape for `GET /api/v1/processing-errors`."""

    items: list[ProcessingErrorItem]
