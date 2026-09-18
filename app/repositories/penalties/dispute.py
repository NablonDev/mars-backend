"""Repository for penalty_dispute."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import PenaltyDispute
from app.models.enums import DisputeStatus

_ACTIVE_STATUSES = (DisputeStatus.OPEN, DisputeStatus.ANALYZED)


def _to_dict(row: PenaltyDispute) -> dict:
    """Serialize a PenaltyDispute row into a dict."""
    return {
        "id": row.id,
        "dispute_number": row.dispute_number,
        "actual_penalty_id": row.actual_penalty_id,
        "purchase_order_id": row.purchase_order_id,
        "rule_id": row.rule_id,
        "reason_code": row.reason_code,
        "claimed_amount": float(row.claimed_amount),
        "computed_amount": float(row.computed_amount) if row.computed_amount is not None else None,
        "delta_amount": float(row.delta_amount) if row.delta_amount is not None else None,
        "verdict": row.verdict,
        "dispute_status": row.dispute_status,
        "analysis_breakdown": row.analysis_breakdown,
        "analyzed_at": row.analyzed_at,
        "resolved_at": row.resolved_at,
        "resolved_by": row.resolved_by,
        "override_verdict": row.override_verdict,
        "override_reason": row.override_reason,
        "notes": row.notes,
        "response_due_date": row.response_due_date,
        "created_at": row.created_at,
    }


class PenaltyDisputeRepository:
    """Access layer for penalty_dispute rows.

    Tracks the retailer-facing dispute lifecycle for a charged penalty:
    OPEN -> ANALYZED (by the deterministic dispute engine) -> RESOLVED or
    OVERRIDDEN (by a human decision).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        actual_penalty_id: UUID,
        purchase_order_id: UUID,
        reason_code: str,
        claimed_amount: float,
        notes: str | None = None,
        response_due_date: date | None = None,
    ) -> dict:
        """Open a new penalty dispute in OPEN status for a charged penalty."""
        row = PenaltyDispute(
            actual_penalty_id=actual_penalty_id,
            purchase_order_id=purchase_order_id,
            reason_code=reason_code,
            claimed_amount=claimed_amount,
            dispute_status=DisputeStatus.OPEN,
            notes=notes,
            response_due_date=response_due_date,
        )
        self._session.add(row)
        self._session.flush()
        return _to_dict(row)

    def _get_row(self, dispute_id: UUID) -> PenaltyDispute | None:
        """Fetch the ORM row for a dispute id, or None if not found."""
        return self._session.scalars(select(PenaltyDispute).where(PenaltyDispute.id == dispute_id)).first()

    def get_by_id(self, dispute_id: UUID) -> dict | None:
        """Fetch a dispute by id, or None if not found."""
        row = self._get_row(dispute_id)
        return _to_dict(row) if row is not None else None

    def get_by_number(self, dispute_number: str) -> dict | None:
        """Fetch a dispute by its business number, or None if not found."""
        row = self._session.scalars(
            select(PenaltyDispute).where(PenaltyDispute.dispute_number == dispute_number)
        ).first()
        return _to_dict(row) if row is not None else None

    def list_for_actual_penalty(self, actual_penalty_id: UUID) -> list[dict]:
        """Return every dispute opened against one charge, oldest first."""
        rows = self._session.scalars(
            select(PenaltyDispute)
            .where(PenaltyDispute.actual_penalty_id == actual_penalty_id)
            .order_by(PenaltyDispute.created_at.asc())
        ).all()
        return [_to_dict(r) for r in rows]

    def find_active_for_actual_penalty(self, actual_penalty_id: UUID) -> dict | None:
        """Return the latest OPEN or ANALYZED dispute for a charge, or None.

        Backs the at-most-one-active-dispute-per-charge rule in
        `DisputeResolutionService.open_dispute`. A charge may accumulate several disputes over
        time: once the prior one is terminal, a new one can be opened.
        """
        row = self._session.scalars(
            select(PenaltyDispute)
            .where(
                PenaltyDispute.actual_penalty_id == actual_penalty_id,
                PenaltyDispute.dispute_status.in_(_ACTIVE_STATUSES),
            )
            .order_by(PenaltyDispute.created_at.desc())
            .limit(1)
        ).first()
        return _to_dict(row) if row is not None else None

    def list_for_purchase_order(self, purchase_order_id: UUID | None = None) -> list[dict]:
        """Return every dispute for one PO, or across all POs when the filter is omitted."""
        query = select(PenaltyDispute)
        if purchase_order_id is not None:
            query = query.where(PenaltyDispute.purchase_order_id == purchase_order_id)
        query = query.order_by(PenaltyDispute.created_at.asc())
        rows = self._session.scalars(query).all()
        return [_to_dict(r) for r in rows]

    def save_verdict(
        self,
        dispute_id: UUID,
        rule_id: UUID,
        computed_amount: float,
        delta_amount: float,
        verdict: str,
        analysis_breakdown: dict,
        analyzed_at: datetime,
    ) -> dict:
        """Persist the engine's verdict, moving the dispute to ANALYZED.

        Re-analyzing the same `dispute_id` overwrites the prior verdict and breakdown
        instead of erroring; `DisputeResolutionService.analyze` decides whether a non-OPEN
        dispute may be re-analyzed at all. Raises `ValueError` for an unknown id.
        """
        row = self._get_row(dispute_id)
        if row is None:
            raise ValueError(f"No penalty dispute found with id={dispute_id!r}")

        row.rule_id = rule_id
        row.computed_amount = computed_amount
        row.delta_amount = delta_amount
        row.verdict = verdict
        row.analysis_breakdown = analysis_breakdown
        row.analyzed_at = analyzed_at
        row.dispute_status = DisputeStatus.ANALYZED
        self._session.flush()
        return _to_dict(row)

    def resolve(
        self,
        dispute_id: UUID,
        dispute_status: str,
        resolved_by: str,
        resolved_at: datetime,
        override_verdict: str | None = None,
        override_reason: str | None = None,
    ) -> dict:
        """Resolve or override a dispute, recording who decided it and when.

        Raises `ValueError` if no row exists for `dispute_id`.
        """
        row = self._get_row(dispute_id)
        if row is None:
            raise ValueError(f"No penalty dispute found with id={dispute_id!r}")

        row.dispute_status = dispute_status
        row.resolved_by = resolved_by
        row.resolved_at = resolved_at
        row.override_verdict = override_verdict
        row.override_reason = override_reason
        self._session.flush()
        return _to_dict(row)

    def truncate_all(self) -> None:
        """Delete every dispute; must run before the actual-penalty, rule and PO truncates."""
        self._session.execute(delete(PenaltyDispute))
        self._session.flush()
