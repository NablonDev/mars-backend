"""Repository for penalty_job_run_context and penalty_job_item_context."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.penalties import PenaltyJobItemContext, PenaltyJobRunContext


def _run_context_to_dict(row: PenaltyJobRunContext) -> dict:
    """Serialize a PenaltyJobRunContext row into a dict."""
    return {
        "job_run_id": row.job_run_id,
        "projection_date": row.projection_date,
        "stacking_mode_override": row.stacking_mode_override,
        "metadata_json": row.metadata_json,
    }


def _item_context_to_dict(row: PenaltyJobItemContext) -> dict:
    """Serialize a PenaltyJobItemContext row into a dict."""
    return {
        "job_item_id": row.job_item_id,
        "purchase_order_id": row.purchase_order_id,
        "projection_date": row.projection_date,
        "task_type": row.task_type,
        "stacking_mode_override": row.stacking_mode_override,
        "force_regenerate_summary": row.force_regenerate_summary,
    }


class PenaltyJobRunContextRepository:
    """Access layer for penalty_job_run_context.

    Holds the per-run parameters (projection date, stacking-mode override,
    arbitrary metadata) a scheduled penalty job was launched with.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        job_run_id: UUID,
        projection_date: date,
        stacking_mode_override: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """Create a penalty_job_run_context row for a job run."""
        row = PenaltyJobRunContext(
            job_run_id=job_run_id,
            projection_date=projection_date,
            stacking_mode_override=stacking_mode_override,
            metadata_json=metadata or {},
        )
        self._session.add(row)
        self._session.flush()
        return _run_context_to_dict(row)

    def get(self, job_run_id: UUID) -> dict | None:
        """Fetch a job run's context by job_run_id, or None if not found."""
        row = self._session.get(PenaltyJobRunContext, job_run_id)
        return _run_context_to_dict(row) if row is not None else None

    def truncate_all(self) -> None:
        """Delete every row; must run before `JobQueueRepository.truncate_all` (FK)."""
        self._session.execute(delete(PenaltyJobRunContext))
        self._session.flush()


class PenaltyJobItemContextRepository:
    """Access layer for penalty_job_item_context.

    Holds the per-PO parameters (task type, stacking-mode override, force-
    regenerate flag) a single job item was dispatched with.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        job_item_id: UUID,
        purchase_order_id: UUID,
        projection_date: date,
        task_type: str,
        stacking_mode_override: str | None = None,
        force_regenerate_summary: bool = False,
    ) -> dict:
        """Create a penalty_job_item_context row for a job item."""
        row = PenaltyJobItemContext(
            job_item_id=job_item_id,
            purchase_order_id=purchase_order_id,
            projection_date=projection_date,
            task_type=task_type,
            stacking_mode_override=stacking_mode_override,
            force_regenerate_summary=force_regenerate_summary,
        )
        self._session.add(row)
        self._session.flush()
        return _item_context_to_dict(row)

    def get(self, job_item_id: UUID) -> dict | None:
        """Fetch a job item's context by job_item_id, or None if not found."""
        row = self._session.get(PenaltyJobItemContext, job_item_id)
        return _item_context_to_dict(row) if row is not None else None

    def truncate_all(self) -> None:
        """Delete every row; must run before the job-item and purchase-order truncates."""
        self._session.execute(delete(PenaltyJobItemContext))
        self._session.flush()
