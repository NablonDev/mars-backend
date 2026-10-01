"""API schemas for the fulfillment-timeline projection engine: runs, risks, and plan detail."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PenaltyTimelineRunRequest(BaseModel):
    """Body for `POST /penalties/timeline/runs`."""

    projection_date: date | None = None


class TimelineRunSummaryResponse(BaseModel):
    """Response shape for one `POST /penalties/timeline/runs` batch run."""

    model_config = ConfigDict(from_attributes=True)

    plans_evaluated: int
    plans_skipped: int
    skipped_plan_ids: list[UUID]
    status_counts: dict[str, int]
    total_projected_penalty: float


class FulfillmentRiskResponse(BaseModel):
    """Response shape for one `fulfillment_risk` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    fulfillment_plan_id: UUID
    purchase_order_id: UUID
    projection_date: date
    risk_type: str
    status: str
    measured_milestone_code: str
    projected_measured_date: date | None
    window_start: date | None
    window_end: date | None
    days_off: int | None
    shortfall_quantity: float | None
    driver_milestone_code: str | None
    driver_reason_code: str | None
    driver_event_id: UUID | None
    projected_penalty_amount: float
    currency_code: str
    priced_rule_ids: list[str]


class FulfillmentMitigationOptionResponse(BaseModel):
    """Response shape for one `fulfillment_mitigation_option` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    fulfillment_plan_id: UUID
    purchase_order_id: UUID
    projection_date: date
    action_code: str
    owner_team: str | None
    feasible: bool
    infeasible_reason: str | None
    act_by_date: date | None
    penalty_before: float
    penalty_after: float | None
    action_cost: float
    net_saving: float | None
    confidence: str
    rank_no: int | None
    addresses_risk_types: list[str]
    rationale: str | None


class FulfillmentPlanLineResponse(BaseModel):
    """Response shape for one `fulfillment_plan_line` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    fulfillment_plan_id: UUID
    purchase_order_line_id: UUID
    planned_quantity: float
    confirmed_quantity: float | None
    shipped_quantity: float | None


class FulfillmentMilestoneResponse(BaseModel):
    """Response shape for one plan milestone, with its projected date alongside baseline/planned/actual.

    `projected_date` is not a `fulfillment_milestone` column -- it is read
    from the plan's latest `fulfillment_risk` row's `projected_milestones`
    for this milestone's `code`, falling back to `planned_date` when no risk
    row exists yet (the plan has never been projected) or the code is
    missing from that row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    baseline_date: date | None
    planned_date: date | None
    projected_date: date | None
    actual_date: date | None
    status: str


class FulfillmentEventResponse(BaseModel):
    """Response shape for one `fulfillment_event` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subject_type: str
    subject_id: UUID
    fulfillment_plan_id: UUID | None
    milestone_type_id: UUID | None
    event_type: str
    field_name: str | None
    old_value: str | None
    new_value: str | None
    reason_code: str | None
    event_at: datetime
    source: str
    source_reference: str | None


class FulfillmentPlanResponse(BaseModel):
    """Response shape for one `fulfillment_plan` header row, no lines/milestones/events."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plan_number: str
    purchase_order_id: UUID
    delivery_id: UUID | None
    ship_from_warehouse_id: UUID | None
    carrier_id: UUID | None
    freight_term: str
    planned_transit_days: int | None
    status: str


class FulfillmentPlanDetailResponse(FulfillmentPlanResponse):
    """Full plan detail: header plus lines, milestones, events, latest risks and mitigation options."""

    lines: list[FulfillmentPlanLineResponse]
    milestones: list[FulfillmentMilestoneResponse]
    events: list[FulfillmentEventResponse]
    risks: list[FulfillmentRiskResponse]
    mitigation_options: list[FulfillmentMitigationOptionResponse]
