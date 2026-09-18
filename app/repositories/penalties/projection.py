"""Repository for penalty_projection and actual_penalty."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import ActualPenalty, PenaltyProjection, PurchaseOrder, Retailer
from app.services.penalties.projection import ProjectionResult


@dataclass(frozen=True)
class _ProjectionRow:
    """One `penalty_projection` row's worth of fields, real or skipped, before insert/update."""

    rule_id: str
    violation_type: str
    probability: float
    penalty_amount: float
    expected_penalty_amount: float
    skip_reason: str | None


def _projection_to_dict(row: PenaltyProjection) -> dict:
    """Serialize a PenaltyProjection row into a dict."""
    return {
        "id": row.id,
        "purchase_order_id": row.purchase_order_id,
        "rule_id": row.rule_id,
        "projection_date": row.projection_date,
        "violation_type": row.violation_type,
        "failure_probability": float(row.failure_probability),
        "penalty_amount": float(row.penalty_amount),
        "expected_penalty_amount": float(row.expected_penalty_amount),
        "days_to_delivery": row.days_to_delivery,
        "projection_status": row.projection_status,
        "skip_reason": row.skip_reason,
    }


def _actual_penalty_to_dict(row: ActualPenalty) -> dict:
    """Serialize an ActualPenalty row into a dict."""
    return {
        "id": row.id,
        "actual_penalty_number": row.actual_penalty_number,
        "purchase_order_id": row.purchase_order_id,
        "purchase_order_line_id": row.purchase_order_line_id,
        "violation_type": row.violation_type,
        "actual_penalty_amount": float(row.actual_penalty_amount),
        "invoice_or_deduction_date": row.invoice_or_deduction_date,
        "dispute_status": row.dispute_status,
        "claim_facts": row.claim_facts,
    }


class PenaltyProjectionRepository:
    """Access layer for penalty_projection facts.

    Handles reads/writes of projected penalties with filtering, aggregation,
    and stacking-aware lookups. All writes are idempotent (replaces on duplicate key).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def save_result(self, purchase_order_id: UUID, result: ProjectionResult) -> dict[str, UUID]:
        """Persist violations and skipped rules as penalty_projection rows, keyed by rule_id.

        A skipped rule (`result.skipped`) writes a zero-amount row carrying `skip_reason`,
        so a rule this run's engine could not price still shows up rather than vanishing.
        """
        entries = [
            _ProjectionRow(
                v.rule_id, v.violation_type, v.probability, v.penalty_amount, v.expected_penalty_amount, None
            )
            for v in result.violations
        ] + [
            _ProjectionRow(s.rule_id, s.violation_type, 0.0, 0.0, 0.0, s.skip_reason) for s in result.skipped
        ]

        rows_by_rule_id: list[tuple[str, PenaltyProjection]] = []
        for entry in entries:
            rule_id = entry.rule_id if isinstance(entry.rule_id, UUID) else UUID(entry.rule_id)
            row = self._upsert(purchase_order_id, rule_id, result, entry)
            rows_by_rule_id.append((str(rule_id), row))
        self._session.flush()
        return {rule_id_str: row.id for rule_id_str, row in rows_by_rule_id}

    def get_by_id(self, projection_id: UUID) -> dict | None:
        """Fetch one `penalty_projection` row by its surrogate id, or None."""
        row = self._session.get(PenaltyProjection, projection_id)
        return _projection_to_dict(row) if row is not None else None

    def list_projections(
        self,
        purchase_order_id: UUID | None = None,
        status: str | None = None,
        projection_date: date | None = None,
        projection_date_from: date | None = None,
        projection_date_to: date | None = None,
    ) -> list[dict]:
        """Filter `penalty_projection` rows for one purchase order, or across all.

        With `purchase_order_id` given, this returns that PO's full matching history
        and the `status` and date filters are additive. Without it, rows are first
        restricted to each PO's own latest matching `projection_date` and only then
        filtered by `status`, so a PO whose only `status` match sits on an older date
        drops out entirely.
        """
        query = select(PenaltyProjection)
        if purchase_order_id is not None:
            query = query.where(PenaltyProjection.purchase_order_id == purchase_order_id)
        if projection_date is not None:
            query = query.where(PenaltyProjection.projection_date == projection_date)
        if projection_date_from is not None:
            query = query.where(PenaltyProjection.projection_date >= projection_date_from)
        if projection_date_to is not None:
            query = query.where(PenaltyProjection.projection_date <= projection_date_to)
        if purchase_order_id is not None and status is not None:
            query = query.where(PenaltyProjection.projection_status == status)
        query = query.order_by(
            PenaltyProjection.projection_date.asc(),
            PenaltyProjection.rule_id.asc(),
        )
        rows = [_projection_to_dict(r) for r in self._session.scalars(query).all()]

        if purchase_order_id is None:
            latest_by_po: dict[UUID, date] = {}
            for row in rows:
                po_id = row["purchase_order_id"]
                if po_id not in latest_by_po or row["projection_date"] > latest_by_po[po_id]:
                    latest_by_po[po_id] = row["projection_date"]
            rows = [r for r in rows if r["projection_date"] == latest_by_po[r["purchase_order_id"]]]
            if status is not None:
                rows = [r for r in rows if r["projection_status"] == status]

        return rows

    def list_history(self, purchase_order_id: UUID) -> list[dict]:
        """Return the full, unfiltered projection history for one PO."""
        return self.list_projections(purchase_order_id=purchase_order_id)

    def get_latest(self, purchase_order_id: UUID) -> dict | None:
        """Fetch latest projection for a PO with stacking totals applied, or None if none exist."""
        history = self.list_history(purchase_order_id)
        if not history:
            return None

        latest_date = max(h["projection_date"] for h in history)
        rows = [h for h in history if h["projection_date"] == latest_date]

        stacking_mode = self._get_stacking_mode(purchase_order_id)
        if stacking_mode == "MAX":
            total = max((r["expected_penalty_amount"] for r in rows), default=0.0)
        else:
            total = sum(r["expected_penalty_amount"] for r in rows)

        return {
            "purchase_order_id": purchase_order_id,
            "projection_date": latest_date,
            "total_expected_penalty_amount": round(total, 2),
            "violations": rows,
        }

    def truncate_all(self) -> None:
        """Delete every projection; must run before the purchase-order and rule truncates."""
        self._session.execute(delete(PenaltyProjection))
        self._session.flush()

    def _upsert(
            self, purchase_order_id: UUID, rule_id: UUID, result: ProjectionResult, entry: _ProjectionRow
        ) -> PenaltyProjection:
        """Insert or replace one `penalty_projection` row for one (PO, rule, projection_date)."""
        existing = self._session.scalars(
            select(PenaltyProjection).where(
                PenaltyProjection.purchase_order_id == purchase_order_id,
                PenaltyProjection.rule_id == rule_id,
                PenaltyProjection.projection_date == result.projection_date,
            )
        ).first()

        if existing is not None:
            existing.violation_type = entry.violation_type
            existing.failure_probability = entry.probability
            existing.penalty_amount = entry.penalty_amount
            existing.expected_penalty_amount = entry.expected_penalty_amount
            existing.days_to_delivery = result.days_to_delivery
            existing.projection_status = "OPEN"
            existing.skip_reason = entry.skip_reason
            return existing

        row = PenaltyProjection(
            purchase_order_id=purchase_order_id,
            rule_id=rule_id,
            projection_date=result.projection_date,
            violation_type=entry.violation_type,
            failure_probability=entry.probability,
            penalty_amount=entry.penalty_amount,
            expected_penalty_amount=entry.expected_penalty_amount,
            days_to_delivery=result.days_to_delivery,
            projection_status="OPEN",
            skip_reason=entry.skip_reason,
        )
        self._session.add(row)
        return row
    
    def _get_stacking_mode(self, purchase_order_id: UUID) -> str:
        """Resolve a purchase order's retailer `stacking_mode`, falling back to SUM.

        Joined here rather than composed from `PurchaseOrderRepository` and
        `MasterDataRepository` because no repository in this codebase depends on
        another.
        """
        stacking_mode = self._session.scalars(
            select(Retailer.stacking_mode)
            .join(PurchaseOrder, PurchaseOrder.retailer_id == Retailer.id)
            .where(PurchaseOrder.id == purchase_order_id)
        ).first()
        return stacking_mode or "SUM"


class ActualPenaltyRepository:
    """Access layer for actual_penalty facts.

    Persists invoiced/deducted penalties with dispute status tracking.
    All writes are idempotent (replaces on duplicate key).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_actual_penalty(
        self,
        actual_penalty_number: str,
        purchase_order_id: UUID,
        violation_type: str,
        actual_penalty_amount: float,
        invoice_or_deduction_date: date,
        dispute_status: str = "NONE",
        purchase_order_line_id: UUID | None = None,
    ) -> dict:
        """Create an actual_penalty row and return it as a dict.

        `dispute_status` leaves its `"NONE"` default only once a `PenaltyDispute` is
        opened against the charge.
        """
        row = ActualPenalty(
            actual_penalty_number=actual_penalty_number,
            purchase_order_id=purchase_order_id,
            violation_type=violation_type,
            actual_penalty_amount=actual_penalty_amount,
            invoice_or_deduction_date=invoice_or_deduction_date,
            dispute_status=dispute_status,
            purchase_order_line_id=purchase_order_line_id,
        )
        self._session.add(row)
        self._session.flush()
        return _actual_penalty_to_dict(row)

    def get(self, actual_penalty_id: UUID) -> dict | None:
        """Fetch one `actual_penalty` row by its surrogate id, or None."""
        row = self._session.get(ActualPenalty, actual_penalty_id)
        return _actual_penalty_to_dict(row) if row is not None else None

    def set_claim_facts(self, actual_penalty_id: UUID, claim_facts: dict) -> dict:
        """Write `claim_facts` once; raises `ValueError` if already set or the id is unknown.

        Write-once by design: a corrected charge needs a new `actual_penalty` row and a
        new dispute cycle, never a second write to this column on the same row.
        """
        row = self._session.get(ActualPenalty, actual_penalty_id)
        if row is None:
            raise ValueError(f"No actual_penalty found with id={actual_penalty_id!r}")
        if row.claim_facts is not None:
            raise ValueError(f"claim_facts already set for actual_penalty_id={actual_penalty_id!r}")
        row.claim_facts = claim_facts
        self._session.flush()
        return _actual_penalty_to_dict(row)

    def list_actual_penalties(self, purchase_order_id: UUID | None = None) -> list[dict]:
        """List actual penalties for one PO, or across all POs when the filter is omitted."""
        query = select(ActualPenalty)
        if purchase_order_id is not None:
            query = query.where(ActualPenalty.purchase_order_id == purchase_order_id)
        rows = self._session.scalars(query).all()
        return [_actual_penalty_to_dict(r) for r in rows]

    def list_for_purchase_order(self, purchase_order_id: UUID) -> list[dict]:
        """Return every actual penalty recorded against one PO."""
        return self.list_actual_penalties(purchase_order_id=purchase_order_id)

    def truncate_all(self) -> None:
        """Delete every actual penalty; must run before the purchase-order truncate."""
        self._session.execute(delete(ActualPenalty))
        self._session.flush()
