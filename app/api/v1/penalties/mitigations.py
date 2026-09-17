"""API endpoints for penalty mitigation options and summaries."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.dependencies import (
    get_mitigation_option_repository,
    get_mitigation_service,
    get_mitigation_summary_service,
    get_penalty_projection_repository,
    get_purchase_order_repository,
    parse_include,
)
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import AppError, NotFoundError, ValidationError
from app.models.enums import SummaryStatus
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.mitigation import MitigationOptionRepository
from app.repositories.penalties.projection import PenaltyProjectionRepository
from app.schemas.penalties.mitigations import (
    MitigationOptionDetailResponse,
    MitigationOptionsResponse,
    PenaltyMitigationRunRequest,
    PenaltyMitigationSummaryRequest,
    PenaltyMitigationSummaryResponse,
    PenaltyMitigationSummaryStatusResponse,
)
from app.services.penalties.mitigation.service import MitigationService
from app.services.penalties.mitigation.summary_service import MitigationSummaryService

router = APIRouter(prefix="/penalties", tags=["penalty-mitigations"])

# Used by the GET routes below (list, get-one). `POST /penalties/mitigations`
# accepts no `include=`: a compute call always returns the bare
# mitigation-options result with `summary` unset, fetched via the GET routes.
_INCLUDE_SUMMARY = parse_include(frozenset({"summary"}))


def _resolve_projection(projections: PenaltyProjectionRepository, projection_id: UUID) -> tuple[UUID, date]:
    """Load a projection and return its purchase order ID and projection date."""
    row = projections.get_by_id(projection_id)
    if row is None:
        raise NotFoundError(
            code="PROJECTION_NOT_FOUND",
            message=f"No penalty projection found with projection_id={projection_id}",
        )
    return row["purchase_order_id"], row["projection_date"]


def _resolve_purchase_order_and_date(
    projections: PenaltyProjectionRepository,
    purchase_orders: PurchaseOrderRepository,
    *,
    projection_id: UUID | None,
    purchase_order_id: UUID | None,
    projection_date: date | None,
) -> tuple[UUID, date]:
    """Resolve either a projection_id or a (purchase_order_id, projection_date) pair."""
    has_projection_id = projection_id is not None
    has_purchase_order_id = purchase_order_id is not None
    has_projection_date = projection_date is not None
    if has_purchase_order_id != has_projection_date:
        raise ValidationError(
            code="VALIDATION_ERROR",
            message="purchase_order_id and projection_date must both be provided together, or neither.",
        )
    has_direct_pair = has_purchase_order_id and has_projection_date
    if has_projection_id == has_direct_pair:
        raise ValidationError(
            code="VALIDATION_ERROR",
            message="Provide exactly one of `projection_id` or (`purchase_order_id` + `projection_date`).",
        )
    if projection_id is not None:
        return _resolve_projection(projections, projection_id)
    assert purchase_order_id is not None
    assert projection_date is not None
    purchase_orders.require_purchase_order(purchase_order_id)
    return purchase_order_id, projection_date


def _try_get_summary_job(service: MitigationSummaryService, purchase_order_id: UUID, as_of_date: date | None):
    """Resolve the cached summary job for one (PO, date), or `None` if there is nothing to show."""
    try:
        return service.get_status(purchase_order_id, as_of_date=as_of_date)
    except AppError:
        return None


def _attach_summary(
    detail: MitigationOptionDetailResponse,
    service: MitigationSummaryService,
    purchase_order_id: UUID,
    projection_date: date,
) -> None:
    """Attach a mitigation summary to a response row if one exists and is ready."""
    job = _try_get_summary_job(service, purchase_order_id, projection_date)
    if job is None:
        return
    detail.summary_status = SummaryStatus(job.status)
    if job.status == SummaryStatus.READY and job.output is not None:
        detail.summary = PenaltyMitigationSummaryResponse.model_validate(job.output)


@router.get("/mitigations", response_model=Envelope[MitigationOptionsResponse])
def list_penalty_mitigations(
    projection_id: UUID | None = Query(default=None),
    purchase_order_id: UUID | None = Query(default=None),
    projection_date: date | None = Query(default=None),
    projections: PenaltyProjectionRepository = Depends(get_penalty_projection_repository),
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    mitigation_options: MitigationOptionRepository = Depends(get_mitigation_option_repository),
    mitigation_summary_service: MitigationSummaryService = Depends(get_mitigation_summary_service),
    include: set[str] = Depends(_INCLUDE_SUMMARY),
) -> Envelope[MitigationOptionsResponse]:
    """List mitigation options for a projection by ID or (purchase_order_id, projection_date)."""
    resolved_purchase_order_id, resolved_projection_date = _resolve_purchase_order_and_date(
        projections,
        purchase_orders,
        projection_id=projection_id,
        purchase_order_id=purchase_order_id,
        projection_date=projection_date,
    )
    options = mitigation_options.list_for_date(resolved_purchase_order_id, resolved_projection_date)
    details = [MitigationOptionDetailResponse.model_validate(o) for o in options]
    if "summary" in include:
        for detail in details:
            _attach_summary(
                detail, mitigation_summary_service, resolved_purchase_order_id, resolved_projection_date
            )
    return success_envelope(
        MitigationOptionsResponse(
            purchase_order_id=resolved_purchase_order_id,
            projection_date=resolved_projection_date,
            options=details,
        )
    )


@router.post(
    "/mitigations",
    response_model=Envelope[MitigationOptionsResponse],
    status_code=status.HTTP_201_CREATED,
)
def run_penalty_mitigations(
    body: PenaltyMitigationRunRequest,
    projections: PenaltyProjectionRepository = Depends(get_penalty_projection_repository),
    mitigation_options: MitigationOptionRepository = Depends(get_mitigation_option_repository),
    mitigation_service: MitigationService = Depends(get_mitigation_service),
) -> Envelope[MitigationOptionsResponse]:
    """Compute and persist mitigation options for a projection."""
    # `PenaltyMitigationRunRequest`'s own model validator already guarantees
    # exactly one of the two shapes was supplied.
    if body.projection_id is not None:
        purchase_order_id, projection_date = _resolve_projection(projections, body.projection_id)
    else:
        assert body.purchase_order_id is not None
        assert body.projection_date is not None
        purchase_order_id, projection_date = body.purchase_order_id, body.projection_date

    resolved_date, _ = mitigation_service.run_for_purchase_order(purchase_order_id, projection_date)
    # `run_for_purchase_order` returns pure-engine `MitigationOption`
    # dataclasses, which carry no `id`/`purchase_order_id`/`projection_date`.
    # Re-reading the rows it just persisted keeps this response shape
    # consistent with the sibling GET route, at the cost of one extra read.
    options = mitigation_options.list_for_date(purchase_order_id, resolved_date)
    details = [MitigationOptionDetailResponse.model_validate(o) for o in options]
    return success_envelope(
        MitigationOptionsResponse(
            purchase_order_id=purchase_order_id,
            projection_date=resolved_date,
            options=details,
        ),
        message="Mitigation options computed.",
    )


@router.post(
    "/mitigations/summary",
    response_model=Envelope[PenaltyMitigationSummaryStatusResponse],
    responses={202: {"model": Envelope[PenaltyMitigationSummaryStatusResponse]}},
)
def trigger_penalty_mitigation_summary(
    body: PenaltyMitigationSummaryRequest,
    response: Response,
    mitigation_summary_service: MitigationSummaryService = Depends(get_mitigation_summary_service),
) -> Envelope[PenaltyMitigationSummaryStatusResponse]:
    """Request or retrieve a summary of mitigation options, returning 202 if queued."""
    job = mitigation_summary_service.get_or_schedule(
        body.purchase_order_id, as_of_date=body.as_of_date, force_regenerate=body.force_regenerate
    )

    if job.status == SummaryStatus.READY:
        assert job.output is not None
        return success_envelope(
            PenaltyMitigationSummaryStatusResponse(
                purchase_order_id=body.purchase_order_id,
                as_of_date=job.as_of_date,
                status=SummaryStatus.READY,
                summary=PenaltyMitigationSummaryResponse.model_validate(job.output),
            )
        )

    response.status_code = 202
    return success_envelope(
        PenaltyMitigationSummaryStatusResponse(
            purchase_order_id=body.purchase_order_id,
            as_of_date=job.as_of_date,
            status=SummaryStatus.PENDING,
        ),
        message="Penalty mitigation summary generation queued.",
    )


@router.get(
    "/mitigations/summary",
    response_model=Envelope[PenaltyMitigationSummaryStatusResponse],
)
def get_penalty_mitigation_summary(
    purchase_order_id: UUID = Query(...),
    as_of_date: date | None = Query(default=None),
    mitigation_summary_service: MitigationSummaryService = Depends(get_mitigation_summary_service),
) -> Envelope[PenaltyMitigationSummaryStatusResponse]:
    """Poll the status of a mitigation summary job without refetching options."""
    try:
        job = mitigation_summary_service.get_status(purchase_order_id, as_of_date=as_of_date)
    except NotFoundError as exc:
        if exc.code != "NO_MITIGATION_SUMMARY_JOB_EXISTS":
            raise
        return success_envelope(
            PenaltyMitigationSummaryStatusResponse(
                purchase_order_id=purchase_order_id,
                as_of_date=as_of_date,
                status=None,
            )
        )
    return success_envelope(
        PenaltyMitigationSummaryStatusResponse(
            purchase_order_id=purchase_order_id,
            as_of_date=job.as_of_date,
            status=SummaryStatus(job.status),
            summary=(
                PenaltyMitigationSummaryResponse.model_validate(job.output)
                if job.status == SummaryStatus.READY and job.output is not None
                else None
            ),
        )
    )


# NOTE: `GET /penalties/mitigations/{mitigation_id}` MUST stay registered after
# the two literal `/penalties/mitigations/summary` routes above. Starlette
# matches in registration order, and `{mitigation_id}` greedily matches the
# literal `summary` segment too, which would turn that route into a 422
# UUID-parse failure.
@router.get(
    "/mitigations/{mitigation_id}",
    response_model=Envelope[MitigationOptionDetailResponse],
)
def get_penalty_mitigation(
    mitigation_id: UUID,
    mitigation_options: MitigationOptionRepository = Depends(get_mitigation_option_repository),
    mitigation_summary_service: MitigationSummaryService = Depends(get_mitigation_summary_service),
    include: set[str] = Depends(_INCLUDE_SUMMARY),
) -> Envelope[MitigationOptionDetailResponse]:
    """Return a mitigation option without triggering summary generation."""
    row = mitigation_options.get_by_id(mitigation_id)
    if row is None:
        raise NotFoundError(
            code="MITIGATION_OPTION_NOT_FOUND",
            message=f"No mitigation option found with mitigation_id={mitigation_id}",
        )
    detail = MitigationOptionDetailResponse.model_validate(row)
    if "summary" in include:
        _attach_summary(detail, mitigation_summary_service, row["purchase_order_id"], row["projection_date"])
    return success_envelope(detail)
