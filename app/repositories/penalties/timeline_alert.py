"""Repository for penalties.timeline_alert: the ops-tracked alert per (plan, risk type)."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models import FulfillmentPlan, TimelineAlert
from app.services.penalties.timeline.alerts import AlertChange

_CREATE = "CREATE"


def _alert_to_dict(row: TimelineAlert) -> dict:
    """Serialize a TimelineAlert row into a dict, keyed for both `AlertState` and general callers."""
    return {
        "id": row.id,
        "alert_id": row.id,
        "fulfillment_plan_id": row.fulfillment_plan_id,
        "purchase_order_id": row.purchase_order_id,
        "risk_type": row.risk_type,
        "status": row.status,
        "is_tracking": row.is_tracking,
        "change_type": row.change_type,
        "first_seen_date": row.first_seen_date,
        "last_seen_date": row.last_seen_date,
        "prev_penalty_amount": (
            float(row.prev_penalty_amount) if row.prev_penalty_amount is not None else None
        ),
        "last_penalty_amount": float(row.last_penalty_amount),
        "prev_days_off": row.prev_days_off,
        "last_days_off": row.last_days_off,
        "prev_shortfall_quantity": (
            float(row.prev_shortfall_quantity) if row.prev_shortfall_quantity is not None else None
        ),
        "last_shortfall_quantity": (
            float(row.last_shortfall_quantity) if row.last_shortfall_quantity is not None else None
        ),
        "assigned_to": row.assigned_to,
        "chosen_action_code": row.chosen_action_code,
        "notes": row.notes,
        "acknowledged_at": row.acknowledged_at,
        "action_taken_at": row.action_taken_at,
        "closed_at": row.closed_at,
        "acknowledged_by": row.acknowledged_by,
        "action_taken_by": row.action_taken_by,
        "closed_by": row.closed_by,
        "closed_reason": row.closed_reason,
        "actual_penalty_amount": (
            float(row.actual_penalty_amount) if row.actual_penalty_amount is not None else None
        ),
    }


class TimelineAlertRepository:
    """Access layer for `penalties.timeline_alert`.

    `apply_changes` is the only writer: it fans a batch of `AlertChange`
    instructions (from `plan_alert_changes`) out into inserts/updates on this
    table, one call per plan per engine run.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def apply_changes(self, plan_id: UUID, purchase_order_id: UUID, changes: Sequence[AlertChange]) -> None:
        """Apply a plan's alert changes for one run: insert every CREATE, update everything else."""
        for change in changes:
            if change.kind == _CREATE:
                self._session.add(
                    TimelineAlert(
                        fulfillment_plan_id=plan_id,
                        purchase_order_id=purchase_order_id,
                        risk_type=change.risk_type,
                        **change.fields,
                    )
                )
            else:
                assert change.alert_id is not None
                row = self._require_row(change.alert_id)
                for field_name, value in change.fields.items():
                    setattr(row, field_name, value)
        self._session.flush()

    def list_tracking_for_plan(self, plan_id: UUID) -> list[dict]:
        """List a plan's currently-tracked alerts, `AlertState`-shaped for `plan_alert_changes`."""
        rows = self._session.scalars(
            select(TimelineAlert).where(
                TimelineAlert.fulfillment_plan_id == plan_id, TimelineAlert.is_tracking.is_(True)
            )
        ).all()
        return [_alert_to_dict(r) for r in rows]

    def list_for_plan(self, plan_id: UUID) -> list[dict]:
        """List every alert (tracking or not) ever raised for one plan."""
        rows = self._session.scalars(
            select(TimelineAlert)
            .where(TimelineAlert.fulfillment_plan_id == plan_id)
            .order_by(TimelineAlert.risk_type.asc(), TimelineAlert.first_seen_date.asc())
        ).all()
        return [_alert_to_dict(r) for r in rows]

    def get(self, alert_id: UUID | str) -> dict:
        """Fetch one alert by id, raising `NotFoundError` if it doesn't exist."""
        return _alert_to_dict(self._require_row(alert_id))

    def delete_seed_data(self, plan_number_prefix: str) -> None:
        """Delete every alert row for a plan whose `plan_number` matches this prefix."""
        plan_like = f"{plan_number_prefix}%"
        plan_ids = select(FulfillmentPlan.id).where(FulfillmentPlan.plan_number.like(plan_like))
        self._session.execute(delete(TimelineAlert).where(TimelineAlert.fulfillment_plan_id.in_(plan_ids)))
        self._session.flush()

    def _require_row(self, alert_id: UUID | str) -> TimelineAlert:
        """Fetch a `TimelineAlert` row by id, raising `NotFoundError` if it doesn't exist."""
        row = self._session.get(TimelineAlert, alert_id)
        if row is None:
            raise NotFoundError(
                code="TIMELINE_ALERT_NOT_FOUND", message=f"No timeline alert found with alert_id={alert_id}"
            )
        return row
