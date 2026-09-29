"""Repository for mitigation_input and mitigation_option."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import MitigationInput
from app.models import MitigationOption as MitigationOptionModel
from app.services.penalties.mitigation.types import MitigationInputs, ShortageCause
from app.services.penalties.mitigation.types import MitigationOption as MitigationOptionValue


def _row_to_inputs(row: MitigationInput, purchase_order_id: UUID) -> MitigationInputs:
    """Convert a MitigationInput row into a MitigationInputs value object."""
    return MitigationInputs(
        order_id=str(purchase_order_id),
        shortage_cause=ShortageCause(row.shortage_cause),
        shortage_cause_confirmed=row.shortage_cause_confirmed,
        capacity_boost_cost_per_unit=(
            float(row.capacity_boost_cost_per_unit) if row.capacity_boost_cost_per_unit is not None else None
        ),
        capacity_boost_max_units_per_day=(
            float(row.capacity_boost_max_units_per_day)
            if row.capacity_boost_max_units_per_day is not None
            else None
        ),
        capacity_boost_data_confirmed=row.capacity_boost_data_confirmed,
        express_carrier_cost=(
            float(row.express_carrier_cost) if row.express_carrier_cost is not None else None
        ),
        express_carrier_transit_days=row.express_carrier_transit_days,
        express_carrier_data_confirmed=row.express_carrier_data_confirmed,
        split_shipment_handling_cost=float(row.split_shipment_handling_cost),
    )


class MitigationInputRepository:
    """Access layer for mitigation_input data.

    Manages mitigation inputs (shortage cause, capacity/carrier/split options)
    per purchase order, with idempotent upsert semantics.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _get_row(self, purchase_order_id: UUID) -> MitigationInput | None:
        """Fetch the mitigation_input row for a PO, or None if not found."""
        return self._session.scalars(
            select(MitigationInput).where(MitigationInput.purchase_order_id == purchase_order_id)
        ).first()

    def list_purchase_order_ids(self) -> list[UUID]:
        """PO ids that already have a mitigation-input row, for idempotent seeding."""
        return list(self._session.scalars(select(MitigationInput.purchase_order_id)).all())

    def get_inputs(self, purchase_order_id: UUID) -> MitigationInputs:
        """Fetch mitigation inputs for a PO, defaulting to all-unknown if none exist."""
        row = self._get_row(purchase_order_id)
        if row is None:
            return MitigationInputs(order_id=str(purchase_order_id))
        return _row_to_inputs(row, purchase_order_id)

    def upsert_inputs(self, purchase_order_id: UUID, **fields: Any) -> None:
        """Create or update mitigation inputs for a PO (idempotent).

        A partial update: only the keys present in `fields` are touched, so
        callers can set e.g. just `shortage_cause` without clobbering
        previously recorded carrier/capacity data for the same PO.
        """
        row = self._get_row(purchase_order_id)
        if row is None:
            self._session.add(MitigationInput(purchase_order_id=purchase_order_id, **fields))
        else:
            for key, value in fields.items():
                setattr(row, key, value)
        self._session.flush()

    def truncate_all(self) -> None:
        """Delete mitigation options and inputs; both must precede the PO truncate."""
        self._session.execute(delete(MitigationOptionModel))
        self._session.execute(delete(MitigationInput))
        self._session.flush()


def _option_to_dict(row: MitigationOptionModel) -> dict:
    """Serialize a MitigationOptionModel row into a dict."""
    return {
        "id": row.id,
        "purchase_order_id": row.purchase_order_id,
        "projection_date": row.projection_date,
        "action": row.action,
        "projected_penalty_after": float(row.projected_penalty_after),
        "action_cost": float(row.action_cost),
        "net_saving": float(row.net_saving),
        "risk_level": row.risk_level,
        "confidence": row.confidence,
        "rationale": row.rationale,
    }


class MitigationOptionRepository:
    """Access layer for mitigation_option facts.

    Persists ranked mitigation actions from penalty mitigation engine.
    Keyed by (purchase_order_id, projection_date, action) with idempotent writes.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def commit(self) -> None:
        """Commit the session, flushing pending changes."""
        self._session.commit()

    def _find(
        self, purchase_order_id: UUID, projection_date: date, action: str
    ) -> MitigationOptionModel | None:
        """Fetch a mitigation_option by (PO, date, action), or None if not found."""
        return self._session.scalars(
            select(MitigationOptionModel).where(
                MitigationOptionModel.purchase_order_id == purchase_order_id,
                MitigationOptionModel.projection_date == projection_date,
                MitigationOptionModel.action == action,
            )
        ).first()

    def save_results(
        self,
        purchase_order_id: UUID,
        projection_date: date,
        options: list[MitigationOptionValue],
    ) -> list[dict]:
        """Upsert one row per option, keyed on (purchase_order_id, projection_date, action).

        Nothing is ever deleted: an action that stops being eligible on a later run
        simply stops being written, and its earlier row remains as history.
        """
        rows: list[dict] = []
        for option in options:
            existing = self._find(purchase_order_id, projection_date, option.action)
            if existing is not None:
                existing.projected_penalty_after = option.projected_penalty_after
                existing.action_cost = option.action_cost
                existing.net_saving = option.net_saving
                existing.risk_level = option.risk_level
                existing.confidence = option.confidence
                existing.rationale = option.rationale
                self._session.flush()
                rows.append(_option_to_dict(existing))
                continue

            row = MitigationOptionModel(
                purchase_order_id=purchase_order_id,
                projection_date=projection_date,
                action=option.action,
                projected_penalty_after=option.projected_penalty_after,
                action_cost=option.action_cost,
                net_saving=option.net_saving,
                risk_level=option.risk_level,
                confidence=option.confidence,
                rationale=option.rationale,
            )
            self._session.add(row)
            self._session.flush()
            rows.append(_option_to_dict(row))

        return rows

    def get_by_id(self, mitigation_option_id: UUID) -> dict | None:
        """Fetch one `mitigation_option` row by its surrogate id, or None."""
        row = self._session.get(MitigationOptionModel, mitigation_option_id)
        return _option_to_dict(row) if row is not None else None

    def list_for_date(self, purchase_order_id: UUID, projection_date: date) -> list[dict]:
        """Fetch ranked options for one exact date (by net_saving, descending)."""
        rows = self._session.scalars(
            select(MitigationOptionModel).where(
                MitigationOptionModel.purchase_order_id == purchase_order_id,
                MitigationOptionModel.projection_date == projection_date,
            )
        ).all()
        return sorted((_option_to_dict(r) for r in rows), key=lambda r: r["net_saving"], reverse=True)

    def _latest_date_not_after(self, purchase_order_id: UUID, not_after: date | None = None) -> date | None:
        """Resolve the most recent projection_date, optionally bounded by not_after."""
        stmt = select(func.max(MitigationOptionModel.projection_date)).where(
            MitigationOptionModel.purchase_order_id == purchase_order_id,
        )
        if not_after is not None:
            stmt = stmt.where(MitigationOptionModel.projection_date <= not_after)
        return self._session.scalar(stmt)

    def get_latest(self, purchase_order_id: UUID) -> list[dict]:
        """Fetch ranked options for the most recent projection_date on record."""
        latest_date = self._latest_date_not_after(purchase_order_id)
        if latest_date is None:
            return []
        return self.list_for_date(purchase_order_id, latest_date)

    def get_latest_not_after(self, purchase_order_id: UUID, as_of_date: date) -> list[dict]:
        """Fetch ranked options for the most recent projection_date <= as_of_date."""
        latest_date = self._latest_date_not_after(purchase_order_id, not_after=as_of_date)
        if latest_date is None:
            return []
        return self.list_for_date(purchase_order_id, latest_date)

    def earliest_date(self, purchase_order_id: UUID) -> date | None:
        """Fetch the earliest projection_date on record for a PO, or None if none exist."""
        return self._session.scalar(
            select(func.min(MitigationOptionModel.projection_date)).where(
                MitigationOptionModel.purchase_order_id == purchase_order_id
            )
        )

    def latest_date(self, purchase_order_id: UUID) -> date | None:
        """Fetch the latest projection_date on record for a PO, or None if none exist."""
        return self._session.scalar(
            select(func.max(MitigationOptionModel.projection_date)).where(
                MitigationOptionModel.purchase_order_id == purchase_order_id
            )
        )
