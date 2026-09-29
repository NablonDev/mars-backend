"""Repository for penalty_summary, merged from projection and mitigation summaries."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PenaltySummary
from app.models.enums import SummaryStatus
from app.models.penalties import PenaltyJobItemContext


def _to_dict(row: PenaltySummary) -> dict:
    """Serialize a PenaltySummary row into a dict."""
    return {
        "id": row.id,
        "purchase_order_id": row.purchase_order_id,
        "summary_type": row.summary_type,
        "as_of_date": row.as_of_date,
        "agent_id": row.agent_id,
        "context_hash": row.context_hash,
        "content_fingerprint": row.content_fingerprint,
        "source_as_of_date": row.source_as_of_date,
        "status": row.status,
        "model_name": row.model_name,
        "summary": row.summary,
        "error_message": row.error_message,
        "created_at": row.created_at,
    }


class PenaltySummaryRepository:
    """Access layer for penalty_summary facts.

    Manages projection, mitigation, and dispute summaries with caching,
    reuse tracking, and lifecycle state (PENDING, READY, FAILED).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def commit(self) -> None:
        """Commit the session, flushing pending changes."""
        self._session.commit()

    # ------------------------------------------------------------------
    # PROJECTION / MITIGATION: keyed on (purchase_order_id, summary_type,
    # as_of_date), unchanged by DISPUTE's addition.
    # ------------------------------------------------------------------

    def _find(self, purchase_order_id: UUID, summary_type: str, as_of_date: date) -> PenaltySummary | None:
        """Fetch a summary by (PO, type, date), regardless of status.

        Shared by `create_pending`, `mark_ready`, `mark_failed` and `create_reused`,
        each of which needs the row in whatever state it is in to choose between
        updating in place and inserting. `get_by_key` is the read-only counterpart.
        """
        return self._session.scalars(
            select(PenaltySummary).where(
                PenaltySummary.purchase_order_id == purchase_order_id,
                PenaltySummary.summary_type == summary_type,
                PenaltySummary.as_of_date == as_of_date,
            )
        ).first()

    def get_cached(self, purchase_order_id: UUID, summary_type: str, as_of_date: date) -> dict | None:
        """Fetch a READY summary by (PO, type, date), or None if not found or not READY."""
        row = self._session.scalars(
            select(PenaltySummary).where(
                PenaltySummary.purchase_order_id == purchase_order_id,
                PenaltySummary.summary_type == summary_type,
                PenaltySummary.as_of_date == as_of_date,
                PenaltySummary.status == SummaryStatus.READY,
            )
        ).first()
        return _to_dict(row) if row is not None else None

    def get_by_key(self, purchase_order_id: UUID, summary_type: str, as_of_date: date) -> dict | None:
        """Fetch a summary by (PO, type, date) in any status, or None; see `get_cached`."""
        row = self._find(purchase_order_id, summary_type, as_of_date)
        return _to_dict(row) if row is not None else None

    def get_latest_not_after(
        self, purchase_order_id: UUID, summary_type: str, as_of_date: date
    ) -> dict | None:
        """Return the latest row at or before `as_of_date`, whatever its status.

        Deliberately status-agnostic: a PENDING or FAILED row dated earlier must still
        surface as that job's real status rather than as "no job exists". Ordering on
        `as_of_date` alone also lets a newer PENDING row win over an older READY one;
        the fingerprint-matched reuse case belongs to `find_reusable`.
        """
        row = self._session.scalars(
            select(PenaltySummary)
            .where(
                PenaltySummary.purchase_order_id == purchase_order_id,
                PenaltySummary.summary_type == summary_type,
                PenaltySummary.as_of_date <= as_of_date,
            )
            .order_by(PenaltySummary.as_of_date.desc())
        ).first()
        return _to_dict(row) if row is not None else None

    def create_pending(
        self,
        purchase_order_id: UUID,
        summary_type: str,
        as_of_date: date,
        agent_id: UUID,
        context_hash: str,
        content_fingerprint: str | None = None,
    ) -> dict:
        """Create a PENDING summary, or reset an existing one to PENDING for regeneration."""
        existing = self._find(purchase_order_id, summary_type, as_of_date)

        if existing is not None:
            return self._reset_to_pending(existing, agent_id, context_hash, content_fingerprint)

        row = PenaltySummary(
            purchase_order_id=purchase_order_id,
            summary_type=summary_type,
            as_of_date=as_of_date,
            agent_id=agent_id,
            context_hash=context_hash,
            content_fingerprint=content_fingerprint,
            status=SummaryStatus.PENDING,
        )
        self._session.add(row)

        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            existing = self._find(purchase_order_id, summary_type, as_of_date)
            if existing is None:
                raise
            return self._reset_to_pending(existing, agent_id, context_hash, content_fingerprint)

        return _to_dict(row)

    def _reset_to_pending(
        self,
        row: PenaltySummary,
        agent_id: UUID,
        context_hash: str,
        content_fingerprint: str | None = None,
    ) -> dict:
        """Reset an existing summary to PENDING state, clearing generation results."""
        row.agent_id = agent_id
        row.context_hash = context_hash
        row.content_fingerprint = content_fingerprint
        # Re-arming starts a fresh generation and clears reuse lineage.
        row.source_as_of_date = None
        row.status = SummaryStatus.PENDING
        row.model_name = None
        row.summary = None
        row.error_message = None
        self._session.flush()
        return _to_dict(row)

    def mark_ready(
        self,
        purchase_order_id: UUID,
        summary_type: str,
        as_of_date: date,
        agent_id: UUID,
        model_name: str,
        summary: str,
        content_fingerprint: str | None = None,
    ) -> dict:
        """Mark a summary as READY after fresh generation, creating if missing."""
        row = self._find(purchase_order_id, summary_type, as_of_date)

        if row is None:
            row = PenaltySummary(
                purchase_order_id=purchase_order_id,
                summary_type=summary_type,
                as_of_date=as_of_date,
                agent_id=agent_id,
                context_hash="",
            )
            self._session.add(row)

        row.agent_id = agent_id
        row.status = SummaryStatus.READY
        row.model_name = model_name
        row.summary = summary
        row.error_message = None
        row.content_fingerprint = content_fingerprint
        # A fresh generation has no reuse source.
        row.source_as_of_date = None
        self._session.flush()

        return _to_dict(row)

    def find_reusable(
        self,
        purchase_order_id: UUID,
        summary_type: str,
        agent_id: UUID,
        content_fingerprint: str,
        earliest_source_date: date,
        *,
        not_after: date | None = None,
    ) -> dict | None:
        """Find the latest READY row with matching agent and content.

        The effective source date is `source_as_of_date`, falling back to
        `as_of_date` for a freshly generated row. `not_after` prevents
        reusing a summary generated after the requested date. `agent_id`
        is the reuse-eligibility filter `prompt_version` used to serve
        before `agent`/`prompt_version` merged (see this module's
        docstring).
        """
        effective_source_date = func.coalesce(PenaltySummary.source_as_of_date, PenaltySummary.as_of_date)

        conditions = [
            PenaltySummary.purchase_order_id == purchase_order_id,
            PenaltySummary.summary_type == summary_type,
            PenaltySummary.agent_id == agent_id,
            PenaltySummary.content_fingerprint == content_fingerprint,
            PenaltySummary.status == SummaryStatus.READY,
            effective_source_date >= earliest_source_date,
        ]
        if not_after is not None:
            conditions.append(effective_source_date <= not_after)

        row = self._session.scalars(
            select(PenaltySummary).where(*conditions).order_by(effective_source_date.desc())
        ).first()

        return _to_dict(row) if row is not None else None

    def create_reused(
        self,
        purchase_order_id: UUID,
        summary_type: str,
        as_of_date: date,
        agent_id: UUID,
        context_hash: str,
        content_fingerprint: str,
        model_name: str,
        summary: str,
        source_as_of_date: date,
    ) -> dict:
        """Create or update a READY row using an existing summary.

        `source_as_of_date` is the summary's original generation date,
        preserved across reuse chains.
        """
        existing = self._find(purchase_order_id, summary_type, as_of_date)

        if existing is not None:
            return self._apply_reused_fields(
                existing,
                agent_id=agent_id,
                context_hash=context_hash,
                content_fingerprint=content_fingerprint,
                model_name=model_name,
                summary=summary,
                source_as_of_date=source_as_of_date,
            )

        row = PenaltySummary(
            purchase_order_id=purchase_order_id,
            summary_type=summary_type,
            as_of_date=as_of_date,
            agent_id=agent_id,
            context_hash=context_hash,
            content_fingerprint=content_fingerprint,
            source_as_of_date=source_as_of_date,
            status=SummaryStatus.READY,
            model_name=model_name,
            summary=summary,
        )
        self._session.add(row)

        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            existing = self._find(purchase_order_id, summary_type, as_of_date)
            if existing is None:
                raise
            return self._apply_reused_fields(
                existing,
                agent_id=agent_id,
                context_hash=context_hash,
                content_fingerprint=content_fingerprint,
                model_name=model_name,
                summary=summary,
                source_as_of_date=source_as_of_date,
            )

        return _to_dict(row)

    def _apply_reused_fields(
        self,
        row: PenaltySummary,
        *,
        agent_id: UUID,
        context_hash: str,
        content_fingerprint: str,
        model_name: str,
        summary: str,
        source_as_of_date: date,
    ) -> dict:
        """Apply reused summary fields to an existing row and mark it READY."""
        row.agent_id = agent_id
        row.context_hash = context_hash
        row.content_fingerprint = content_fingerprint
        row.source_as_of_date = source_as_of_date
        row.status = SummaryStatus.READY
        row.model_name = model_name
        row.summary = summary
        row.error_message = None
        self._session.flush()
        return _to_dict(row)

    def mark_failed(
        self,
        purchase_order_id: UUID,
        summary_type: str,
        as_of_date: date,
        agent_id: UUID,
        error_message: str,
    ) -> dict:
        """Mark a summary as FAILED after generation error, creating if missing."""
        row = self._find(purchase_order_id, summary_type, as_of_date)

        if row is None:
            row = PenaltySummary(
                purchase_order_id=purchase_order_id,
                summary_type=summary_type,
                as_of_date=as_of_date,
                agent_id=agent_id,
                context_hash="",
            )
            self._session.add(row)

        row.agent_id = agent_id
        row.status = SummaryStatus.FAILED
        row.error_message = error_message
        row.model_name = None
        row.summary = None
        self._session.flush()

        return _to_dict(row)

    # ------------------------------------------------------------------
    # Recovery sweep (see app.workers.fine_projection)
    # ------------------------------------------------------------------

    def find_stranded_pending(
        self,
        earliest_as_of_date: date,
        latest_as_of_date: date,
        summary_type: str,
    ) -> list[dict]:
        """Find PENDING summaries with no job-item context row for the same PO and date.

        Any task type counts as coverage, terminal jobs included, so the sweep never
        adds a redundant regeneration item beside a live batch item for that (PO, date).
        """
        stmt = (
            select(PenaltySummary.purchase_order_id, PenaltySummary.as_of_date)
            .distinct()
            .outerjoin(
                PenaltyJobItemContext,
                (PenaltyJobItemContext.purchase_order_id == PenaltySummary.purchase_order_id)
                & (PenaltyJobItemContext.projection_date == PenaltySummary.as_of_date),
            )
            .where(
                PenaltySummary.summary_type == summary_type,
                PenaltySummary.status == SummaryStatus.PENDING,
                PenaltySummary.as_of_date >= earliest_as_of_date,
                PenaltySummary.as_of_date <= latest_as_of_date,
                PenaltyJobItemContext.job_item_id.is_(None),
            )
        )
        rows = self._session.execute(stmt).all()
        return [{"purchase_order_id": row.purchase_order_id, "as_of_date": row.as_of_date} for row in rows]

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def truncate_all(self) -> None:
        """Delete every summary row; must run before the purchase-order truncate."""
        self._session.execute(delete(PenaltySummary))
        self._session.flush()
