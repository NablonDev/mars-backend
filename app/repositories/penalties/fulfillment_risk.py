"""Repository for fulfillment_risk and fulfillment_mitigation_option."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import FulfillmentMitigationOption, FulfillmentRisk, PurchaseOrder


def _risk_to_dict(row: FulfillmentRisk) -> dict:
    """Serialize a FulfillmentRisk row into a dict."""
    return {
        "id": row.id,
        "fulfillment_plan_id": row.fulfillment_plan_id,
        "purchase_order_id": row.purchase_order_id,
        "projection_date": row.projection_date,
        "risk_type": row.risk_type,
        "status": row.status,
        "measured_milestone_code": row.measured_milestone_code,
        "projected_measured_date": row.projected_measured_date,
        "window_start": row.window_start,
        "window_end": row.window_end,
        "days_off": row.days_off,
        "shortfall_quantity": float(row.shortfall_quantity) if row.shortfall_quantity is not None else None,
        "driver_milestone_code": row.driver_milestone_code,
        "driver_reason_code": row.driver_reason_code,
        "driver_event_id": row.driver_event_id,
        "projected_penalty_amount": float(row.projected_penalty_amount),
        "currency_code": row.currency_code,
        "priced_rule_ids": row.priced_rule_ids,
        "projected_milestones": row.projected_milestones,
        "calculation_detail": row.calculation_detail,
    }


def _option_to_dict(row: FulfillmentMitigationOption) -> dict:
    """Serialize a FulfillmentMitigationOption row into a dict."""
    return {
        "id": row.id,
        "fulfillment_plan_id": row.fulfillment_plan_id,
        "purchase_order_id": row.purchase_order_id,
        "projection_date": row.projection_date,
        "action_code": row.action_code,
        "owner_team": row.owner_team,
        "feasible": row.feasible,
        "infeasible_reason": row.infeasible_reason,
        "act_by_date": row.act_by_date,
        "penalty_before": float(row.penalty_before),
        "penalty_after": float(row.penalty_after) if row.penalty_after is not None else None,
        "action_cost": float(row.action_cost),
        "net_saving": float(row.net_saving) if row.net_saving is not None else None,
        "confidence": row.confidence,
        "rank_no": row.rank_no,
        "addresses_risk_types": row.addresses_risk_types,
        "rationale": row.rationale,
    }


class FulfillmentRiskRepository:
    """Access layer for fulfillment_risk and fulfillment_mitigation_option.

    Both tables are keyed by (fulfillment_plan_id, projection_date, ...);
    `replace_for_plan_date` is the only writer, making a re-run for the same
    plan/date idempotent rather than accumulating duplicate rows.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def replace_for_plan_date(
        self, plan_id: UUID, projection_date: date, risks: list[dict[str, Any]], options: list[dict[str, Any]]
    ) -> None:
        """Delete then reinsert every risk/option row for one plan on one projection date."""
        self._session.execute(
            delete(FulfillmentMitigationOption).where(
                FulfillmentMitigationOption.fulfillment_plan_id == plan_id,
                FulfillmentMitigationOption.projection_date == projection_date,
            )
        )
        self._session.execute(
            delete(FulfillmentRisk).where(
                FulfillmentRisk.fulfillment_plan_id == plan_id,
                FulfillmentRisk.projection_date == projection_date,
            )
        )
        for risk in risks:
            self._session.add(
                FulfillmentRisk(fulfillment_plan_id=plan_id, projection_date=projection_date, **risk)
            )
        for option in options:
            self._session.add(
                FulfillmentMitigationOption(
                    fulfillment_plan_id=plan_id, projection_date=projection_date, **option
                )
            )
        self._session.flush()

    def delete_seed_data(self, purchase_order_number_prefix: str) -> None:
        """Delete every risk/option row for a PO whose `purchase_order_number` matches this prefix."""
        po_like = f"{purchase_order_number_prefix}%"
        po_ids = select(PurchaseOrder.id).where(PurchaseOrder.purchase_order_number.like(po_like))
        self._session.execute(
            delete(FulfillmentMitigationOption).where(
                FulfillmentMitigationOption.purchase_order_id.in_(po_ids)
            )
        )
        self._session.execute(delete(FulfillmentRisk).where(FulfillmentRisk.purchase_order_id.in_(po_ids)))
        self._session.flush()

    def commit(self) -> None:
        """Commit the session, making the most recent `replace_for_plan_date` durable.

        Called once per plan by `TimelineProjectionService`, so a later plan's
        failure can never roll back an earlier plan's already-persisted rows.
        """
        self._session.commit()

    def list_risks(
        self,
        projection_date: date | None = None,
        status: str | None = None,
        retailer_id: UUID | None = None,
    ) -> list[dict]:
        """List risk rows, narrowed by date/status/retailer.

        With `projection_date` given, returns exactly that date's matching
        rows. Without it, rows are first restricted to each plan's own latest
        matching `projection_date` and only then filtered by `status`, so a
        plan whose only `status` match sits on an older date drops out
        entirely -- the same convention as `PenaltyProjectionRepository.list_projections`.
        """
        query = select(FulfillmentRisk)
        if retailer_id is not None:
            query = query.join(PurchaseOrder, PurchaseOrder.id == FulfillmentRisk.purchase_order_id).where(
                PurchaseOrder.retailer_id == retailer_id
            )
        if projection_date is not None:
            query = query.where(FulfillmentRisk.projection_date == projection_date)
        if projection_date is not None and status is not None:
            query = query.where(FulfillmentRisk.status == status)

        rows = [_risk_to_dict(r) for r in self._session.scalars(query).all()]

        if projection_date is None:
            latest_by_plan: dict[UUID, date] = {}
            for row in rows:
                plan_id = row["fulfillment_plan_id"]
                if plan_id not in latest_by_plan or row["projection_date"] > latest_by_plan[plan_id]:
                    latest_by_plan[plan_id] = row["projection_date"]
            rows = [r for r in rows if r["projection_date"] == latest_by_plan[r["fulfillment_plan_id"]]]
            if status is not None:
                rows = [r for r in rows if r["status"] == status]

        return rows

    def list_for_plan(self, plan_id: UUID, projection_date: date | None = None) -> list[dict]:
        """List risk rows for one plan, narrowed to one projection date when given."""
        query = select(FulfillmentRisk).where(FulfillmentRisk.fulfillment_plan_id == plan_id)
        if projection_date is not None:
            query = query.where(FulfillmentRisk.projection_date == projection_date)
        query = query.order_by(FulfillmentRisk.projection_date.asc(), FulfillmentRisk.risk_type.asc())
        return [_risk_to_dict(r) for r in self._session.scalars(query).all()]

    def list_latest_for_plan(self, plan_id: UUID) -> list[dict]:
        """List one plan's risk rows for its own most recent `projection_date`, or `[]` if never run.

        Unlike `list_risks` (already latest-per-plan when no date is given),
        `list_for_plan` returns every date the plan has ever been run for --
        this resolves the max `projection_date` for one plan first, then
        narrows to just that date's rows.
        """
        latest_date = self._session.scalars(
            select(FulfillmentRisk.projection_date)
            .where(FulfillmentRisk.fulfillment_plan_id == plan_id)
            .order_by(FulfillmentRisk.projection_date.desc())
            .limit(1)
        ).first()
        if latest_date is None:
            return []
        return self.list_for_plan(plan_id, latest_date)

    def list_options_for_plan(self, plan_id: UUID, projection_date: date) -> list[dict]:
        """List mitigation options for one plan on one projection date, ranked first."""
        rows = self._session.scalars(
            select(FulfillmentMitigationOption)
            .where(
                FulfillmentMitigationOption.fulfillment_plan_id == plan_id,
                FulfillmentMitigationOption.projection_date == projection_date,
            )
            .order_by(
                FulfillmentMitigationOption.rank_no.is_(None),
                FulfillmentMitigationOption.rank_no.asc(),
                FulfillmentMitigationOption.action_code.asc(),
            )
        ).all()
        return [_option_to_dict(r) for r in rows]
