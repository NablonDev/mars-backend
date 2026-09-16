"""API schemas for `penalties.penalty_projection` and its summary trigger/poll contract."""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import SummaryStatus
from app.schemas.penalties.mitigations import MitigationOptionResponse, PenaltyMitigationSummaryResponse


class PenaltyProjectionRunRequest(BaseModel):
    """Body for `POST /penalties/projections`."""

    purchase_order_id: UUID
    projection_date: date | None = None
    stacking_mode_override: Literal["SUM", "MAX"] | None = None


class ViolationResponse(BaseModel):
    """One violation produced by a `POST /penalties/projections` run."""

    # One `penalties.penalty_projection` row is written per violation, so this is the id a client
    # needs to reach this run's output without a separate list round-trip. Named to match the
    # `projection_id` param on the routes that consume it, not the bare `id` used elsewhere.
    projection_id: UUID
    violation_type: str
    rule_id: str
    probability: float
    penalty_amount: float
    expected_penalty_amount: float


class PenaltyProjectionResultResponse(BaseModel):
    """Response shape for one `POST /penalties/projections` run.

    The optional `?include=` fields are read from cached state; a run never generates them.
    """

    purchase_order_id: UUID
    projection_date: date
    days_to_delivery: int
    shortage_probability: float
    delay_probability: float
    violations: list[ViolationResponse]
    total_expected_penalty_amount: float
    stacking_mode: str
    summary_status: SummaryStatus | None = None
    summary: PenaltyProjectionSummaryResponse | None = None
    mitigations: list[MitigationOptionResponse] | None = None
    mitigation_summary_status: SummaryStatus | None = None
    mitigation_summary: PenaltyMitigationSummaryResponse | None = None


class PenaltyProjectionHistoryRow(BaseModel):
    """One persisted `penalty_projection` row, as returned by every projection read route."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    purchase_order_id: UUID
    rule_id: UUID
    projection_date: date
    violation_type: str
    failure_probability: float
    penalty_amount: float
    expected_penalty_amount: float
    days_to_delivery: int
    projection_status: str
    skip_reason: str | None = None


class PenaltyExposureResponse(BaseModel):
    """A purchase order's total penalty exposure across its persisted projection violations."""

    purchase_order_id: UUID
    projection_date: date
    total_expected_penalty_amount: float
    violations: list[PenaltyProjectionHistoryRow]


class PenaltyProjectionSummaryRequest(BaseModel):
    """Body for `POST /penalties/projections/summary`."""

    purchase_order_id: UUID
    as_of_date: date | None = None
    force_regenerate: bool = False


class PenaltyProjectionSummaryResponse(BaseModel):
    """Response shape for a generated penalty-projection narrative summary."""

    model_config = ConfigDict(from_attributes=True)

    order_id: str
    as_of_date: date
    prompt_version: str
    model_name: str
    summary: str
    is_reused: bool = False
    generated_for_date: date | None = None
    unchanged_since: date | None = None
    unchanged_for_days: int | None = None


class PenaltyProjectionSummaryStatusResponse(BaseModel):
    """Poll response for a projection summary; a null `status` means never requested, not a 404."""

    purchase_order_id: UUID
    as_of_date: date | None
    status: SummaryStatus | None
    summary: PenaltyProjectionSummaryResponse | None = None
    error_message: str | None = None


class PenaltyProjectionDetailResponse(PenaltyProjectionHistoryRow):
    """One projection row plus its optional `?include=` fields, read from cached state only.

    Every projection route shares the same `{summary, mitigations, mitigation_summary}` allow-list.
    """

    summary_status: SummaryStatus | None = None
    summary: PenaltyProjectionSummaryResponse | None = None
    mitigations: list[MitigationOptionResponse] | None = None
    mitigation_summary_status: SummaryStatus | None = None
    mitigation_summary: PenaltyMitigationSummaryResponse | None = None
