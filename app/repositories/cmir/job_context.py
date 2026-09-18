"""Repository for cmir.cmir_job_run_context and cmir_job_item_context."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.cmir import CmirJobItemContext, CmirJobRunContext


def _run_context_to_dict(row: CmirJobRunContext) -> dict:
    """Project a `CmirJobRunContext` row onto the plain dict shape returned to callers."""
    return {
        "job_run_id": row.job_run_id,
        "source_type": row.source_type,
        "external_batch_id": row.external_batch_id,
        "service_bus_topic": row.service_bus_topic,
        "service_bus_subscription": row.service_bus_subscription,
        "metadata_json": row.metadata_json,
    }


def _item_context_to_dict(row: CmirJobItemContext) -> dict:
    """Project a `CmirJobItemContext` row onto the plain dict shape returned to callers."""
    return {
        "job_item_id": row.job_item_id,
        "email_event_id": row.email_event_id,
        "purchase_order_line_id": row.purchase_order_line_id,
        "metadata_json": row.metadata_json,
    }


class CmirJobRunContextRepository:
    """The CMIR-specific context (batch/topic origin) attached to a shared `process.job_run` row."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        job_run_id: UUID,
        source_type: str,
        external_batch_id: str | None = None,
        service_bus_topic: str | None = None,
        service_bus_subscription: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """Attach CMIR run context to an already-created `job_run_id`."""
        row = CmirJobRunContext(
            job_run_id=job_run_id,
            source_type=source_type,
            external_batch_id=external_batch_id,
            service_bus_topic=service_bus_topic,
            service_bus_subscription=service_bus_subscription,
            metadata_json=metadata or {},
        )
        self._session.add(row)
        self._session.flush()
        return _run_context_to_dict(row)

    def get(self, job_run_id: UUID) -> dict | None:
        """Return the run context for `job_run_id`, or None if none was attached."""
        row = self._session.get(CmirJobRunContext, job_run_id)
        return _run_context_to_dict(row) if row is not None else None


class CmirJobItemContextRepository:
    """The CMIR-specific context (email or PO line) attached to a `process.job_item` row."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        job_item_id: UUID,
        *,
        email_event_id: UUID | None = None,
        purchase_order_line_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """Attach CMIR item context to an already-created `job_item_id`.

        Exactly one of `email_event_id` or `purchase_order_line_id` must be set: a
        job item concerns either an inbound email or a PO line, never both.
        """
        if (email_event_id is None) == (purchase_order_line_id is None):
            raise ValueError(
                "Exactly one of email_event_id/purchase_order_line_id must be set for a job item context."
            )

        row = CmirJobItemContext(
            job_item_id=job_item_id,
            email_event_id=email_event_id,
            purchase_order_line_id=purchase_order_line_id,
            metadata_json=metadata or {},
        )
        self._session.add(row)
        self._session.flush()
        return _item_context_to_dict(row)

    def get(self, job_item_id: UUID) -> dict | None:
        """Return the item context for `job_item_id`, or None if none was attached."""
        row = self._session.get(CmirJobItemContext, job_item_id)
        return _item_context_to_dict(row) if row is not None else None

    def get_by_email_event(self, email_event_id: UUID) -> dict | None:
        """Return the item context for the job item handling `email_event_id`, or None."""
        row = self._session.scalars(
            select(CmirJobItemContext).where(CmirJobItemContext.email_event_id == email_event_id)
        ).first()
        return _item_context_to_dict(row) if row is not None else None
