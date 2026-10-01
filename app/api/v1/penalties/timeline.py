"""API endpoints for the fulfillment-timeline projection engine: runs, risks, and plan detail."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import (
    get_fulfillment_risk_repository,
    get_fulfillment_timeline_repository,
    get_timeline_projection_service,
)
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import NotFoundError
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.penalties.fulfillment_risk import FulfillmentRiskRepository
from app.schemas.penalties.timeline import (
    FulfillmentPlanDetailResponse,
    FulfillmentRiskResponse,
    PenaltyTimelineRunRequest,
    TimelineRunSummaryResponse,
)
from app.services.penalties.timeline.service import TimelineProjectionService

router = APIRouter(prefix="/penalties/timeline", tags=["penalty-timeline"])


@router.post("/runs", response_model=Envelope[TimelineRunSummaryResponse])
def run_penalty_timeline(
    body: PenaltyTimelineRunRequest,
    service: Annotated[TimelineProjectionService, Depends(get_timeline_projection_service)],
) -> Envelope[TimelineRunSummaryResponse]:
    """Project every open/shipped fulfillment plan for one run date, persist, and summarize."""
    summary = service.run_for_all_open(body.projection_date)
    return success_envelope(
        TimelineRunSummaryResponse.model_validate(summary), message="Fulfillment timeline run complete."
    )


@router.get("/risks", response_model=Envelope[list[FulfillmentRiskResponse]])
def list_fulfillment_risks(
    risks: Annotated[FulfillmentRiskRepository, Depends(get_fulfillment_risk_repository)],
    projection_date: Annotated[date | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    retailer_id: Annotated[UUID | None, Query()] = None,
) -> Envelope[list[FulfillmentRiskResponse]]:
    """List the latest fulfillment risks, highest projected penalty first, newest date as tiebreaker."""
    rows = risks.list_risks(projection_date=projection_date, status=status, retailer_id=retailer_id)
    rows.sort(key=lambda r: (r["projected_penalty_amount"], r["projection_date"]), reverse=True)
    return success_envelope([FulfillmentRiskResponse.model_validate(r) for r in rows])


@router.get("/plans/{plan_id}", response_model=Envelope[FulfillmentPlanDetailResponse])
def get_fulfillment_plan(
    plan_id: UUID,
    timeline: Annotated[FulfillmentTimelineRepository, Depends(get_fulfillment_timeline_repository)],
    risks: Annotated[FulfillmentRiskRepository, Depends(get_fulfillment_risk_repository)],
) -> Envelope[FulfillmentPlanDetailResponse]:
    """Return a plan's header, lines, milestones (with projected dates), events, latest risks/options."""
    plan = timeline.get_plan(plan_id)
    if plan is None:
        raise NotFoundError(
            code="FULFILLMENT_PLAN_NOT_FOUND", message=f"No fulfillment plan found with plan_id={plan_id}"
        )

    latest_risks = risks.list_latest_for_plan(plan_id)
    options = risks.list_options_for_plan(plan_id, latest_risks[0]["projection_date"]) if latest_risks else []
    projected_by_code = _latest_projected_dates(latest_risks)
    milestones = [
        {**milestone, "projected_date": projected_by_code.get(milestone["code"], milestone["planned_date"])}
        for milestone in timeline.list_milestones(plan_id)
    ]

    detail = FulfillmentPlanDetailResponse.model_validate(
        {
            **plan,
            "lines": timeline.list_plan_lines(plan_id),
            "milestones": milestones,
            "events": list(reversed(timeline.list_events(plan_id))),
            "risks": latest_risks,
            "mitigation_options": options,
        }
    )
    return success_envelope(detail)


def _latest_projected_dates(latest_risks: list[dict]) -> dict[str, date]:
    """Merge every latest risk row's `projected_milestones` into one {code: projected_date} map.

    Every risk row for the same plan/date carries the same full milestone
    set (see `RiskRowAssembler._milestone_dict`), so in practice the first
    row is enough -- this still merges across rows in case that changes,
    keeping whichever row lists a code first.
    """
    merged: dict[str, date] = {}
    for risk in latest_risks:
        for milestone in risk.get("projected_milestones") or []:
            code = milestone["code"]
            if code not in merged:
                merged[code] = date.fromisoformat(milestone["projected_date"])
    return merged
