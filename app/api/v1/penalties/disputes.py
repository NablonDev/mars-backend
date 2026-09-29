"""API endpoints for post-delivery penalty dispute resolution."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.dependencies import (
    get_dispute_service,
    get_dispute_summary_service,
    get_purchase_order_repository,
)
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import NotFoundError
from app.models.enums import SummaryStatus
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.schemas.penalties.disputes import (
    DisputeOpenRequest,
    DisputeResolveRequest,
    DisputeResponse,
    DisputeSummaryRequest,
    DisputeSummaryResponse,
    DisputeSummaryStatusResponse,
)
from app.services.penalties.dispute.service import DisputeResolutionService
from app.services.penalties.dispute.summary_service import DisputeSummaryService

router = APIRouter(prefix="/penalties", tags=["penalty-disputes"])


@router.post(
    "/disputes",
    response_model=Envelope[DisputeResponse],
    status_code=status.HTTP_201_CREATED,
)
def open_penalty_dispute(
    body: DisputeOpenRequest,
    service: Annotated[DisputeResolutionService, Depends(get_dispute_service)],
) -> Envelope[DisputeResponse]:
    """Open a dispute against an incurred penalty."""
    created = service.open_dispute(
        body.actual_penalty_id,
        body.reason_code,
        body.claimed_amount,
        notes=body.notes,
        claim_facts=body.claim_facts.model_dump(exclude_none=True) if body.claim_facts is not None else None,
    )
    return success_envelope(DisputeResponse.model_validate(created), message="Penalty dispute opened.")


@router.get(
    "/disputes",
    response_model=Envelope[list[DisputeResponse]],
)
def list_penalty_disputes(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    service: Annotated[DisputeResolutionService, Depends(get_dispute_service)],
    purchase_order_id: Annotated[UUID | None, Query()] = None,
) -> Envelope[list[DisputeResponse]]:
    """List penalty disputes, optionally narrowed to one purchase order.

    Naming a purchase order that does not exist is a 404.
    """
    if purchase_order_id is not None:
        purchase_orders.require_purchase_order(purchase_order_id)
    rows = [DisputeResponse.model_validate(r) for r in service.list_for_purchase_order(purchase_order_id)]
    return success_envelope(rows)


@router.get(
    "/disputes/{dispute_id}",
    response_model=Envelope[DisputeResponse],
)
def get_penalty_dispute(
    dispute_id: UUID,
    service: Annotated[DisputeResolutionService, Depends(get_dispute_service)],
) -> Envelope[DisputeResponse]:
    """Retrieve a dispute by its ID."""
    return success_envelope(DisputeResponse.model_validate(service.get(dispute_id)))


@router.post(
    "/disputes/{dispute_id}/analyze",
    response_model=Envelope[DisputeResponse],
)
def analyze_penalty_dispute(
    dispute_id: UUID,
    service: Annotated[DisputeResolutionService, Depends(get_dispute_service)],
) -> Envelope[DisputeResponse]:
    """Compute and persist a dispute verdict synchronously, moving the dispute to ANALYZED."""
    analyzed = service.analyze(dispute_id)
    return success_envelope(DisputeResponse.model_validate(analyzed), message="Penalty dispute analyzed.")


@router.post(
    "/disputes/{dispute_id}/resolve",
    response_model=Envelope[DisputeResponse],
)
def resolve_penalty_dispute(
    dispute_id: UUID,
    body: DisputeResolveRequest,
    service: Annotated[DisputeResolutionService, Depends(get_dispute_service)],
) -> Envelope[DisputeResponse]:
    """Resolve a dispute by accepting, rejecting, or overriding the verdict."""
    resolved = service.resolve(
        dispute_id,
        resolved_by=body.resolved_by,
        override_verdict=body.override_verdict,
        override_reason=body.override_reason,
    )
    return success_envelope(DisputeResponse.model_validate(resolved), message="Penalty dispute resolved.")


@router.post(
    "/disputes/{dispute_id}/summary",
    response_model=Envelope[DisputeSummaryStatusResponse],
    responses={202: {"model": Envelope[DisputeSummaryStatusResponse]}},
)
def trigger_penalty_dispute_summary(
    dispute_id: UUID,
    body: DisputeSummaryRequest,
    response: Response,
    summary_service: Annotated[DisputeSummaryService, Depends(get_dispute_summary_service)],
) -> Envelope[DisputeSummaryStatusResponse]:
    """Request or retrieve a summary analysis of a dispute, returning 202 if queued."""
    job = summary_service.get_or_schedule_for_dispute(dispute_id, force_regenerate=body.force_regenerate)

    if job.status == SummaryStatus.READY:
        assert job.output is not None
        return success_envelope(
            DisputeSummaryStatusResponse(
                dispute_id=dispute_id,
                as_of_date=job.as_of_date,
                status=SummaryStatus.READY,
                summary=DisputeSummaryResponse.model_validate(job.output),
            )
        )

    response.status_code = 202
    return success_envelope(
        DisputeSummaryStatusResponse(
            dispute_id=dispute_id, as_of_date=job.as_of_date, status=SummaryStatus.PENDING
        ),
        message="Penalty dispute summary generation queued.",
    )


@router.get(
    "/disputes/{dispute_id}/summary",
    response_model=Envelope[DisputeSummaryStatusResponse],
)
def get_penalty_dispute_summary(
    dispute_id: UUID,
    summary_service: Annotated[DisputeSummaryService, Depends(get_dispute_summary_service)],
) -> Envelope[DisputeSummaryStatusResponse]:
    """Poll a dispute summary job; never schedules one.

    A dispute with no summary job on record succeeds with a null status rather than 404ing.
    """
    try:
        job = summary_service.get_status_for_dispute(dispute_id)
    except NotFoundError as exc:
        if exc.code != "NO_DISPUTE_SUMMARY_JOB_EXISTS":
            raise
        return success_envelope(
            DisputeSummaryStatusResponse(dispute_id=dispute_id, as_of_date=None, status=None)
        )
    return success_envelope(
        DisputeSummaryStatusResponse(
            dispute_id=dispute_id,
            as_of_date=job.as_of_date,
            status=SummaryStatus(job.status),
            summary=(
                DisputeSummaryResponse.model_validate(job.output)
                if job.status == SummaryStatus.READY and job.output is not None
                else None
            ),
        )
    )
