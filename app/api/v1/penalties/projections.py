"""API endpoints for penalty projections: run, list, read, and trigger summaries."""

from __future__ import annotations

from datetime import date
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.dependencies import (
    get_mitigation_option_repository,
    get_mitigation_summary_service,
    get_penalty_projection_repository,
    get_projection_service,
    get_projection_summary_service,
    get_purchase_order_repository,
    parse_include,
)
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import AppError, NotFoundError
from app.models.enums import SummaryStatus
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.mitigation import MitigationOptionRepository
from app.repositories.penalties.projection import PenaltyProjectionRepository
from app.schemas.penalties.mitigations import MitigationOptionResponse, PenaltyMitigationSummaryResponse
from app.schemas.penalties.projections import (
    PenaltyExposureResponse,
    PenaltyProjectionDetailResponse,
    PenaltyProjectionResultResponse,
    PenaltyProjectionRunRequest,
    PenaltyProjectionSummaryRequest,
    PenaltyProjectionSummaryResponse,
    PenaltyProjectionSummaryStatusResponse,
    ViolationResponse,
)
from app.services.penalties.mitigation.summary_service import MitigationSummaryService
from app.services.penalties.projection.service import ProjectionService
from app.services.penalties.projection.summary_service import ProjectionSummaryService

router = APIRouter(prefix="/penalties", tags=["penalty-projections"])

# Uniform include allow-list across all GET projection routes.
# POST /penalties/projections does not accept include= (always returns bare result).
_INCLUDE_PROJECTION = parse_include(frozenset({"summary", "mitigations", "mitigation_summary"}))


def _require_projection_id(violation) -> UUID:
    """Require that a violation has a persisted projection id."""
    if violation.id is None:
        raise AssertionError(
            f"ViolationProjection for rule_id={violation.rule_id!r} has no persisted id: "
            "was it run through ProjectionService.run_for_purchase_order?"
        )
    return violation.id


def _to_result_response(result) -> PenaltyProjectionResultResponse:
    """Convert a projection result to its HTTP response model.

    Maps every violation onto its persisted projection id along the way.
    """
    return PenaltyProjectionResultResponse(
        purchase_order_id=UUID(result.order_id),
        projection_date=result.projection_date,
        days_to_delivery=result.days_to_delivery,
        shortage_probability=result.shortage_probability,
        delay_probability=result.delay_probability,
        violations=[
            ViolationResponse(
                # `run_for_purchase_order` stitches this onto every violation
                # immediately after persisting it, so it is never `None` here.
                projection_id=_require_projection_id(v),
                violation_type=v.violation_type,
                rule_id=v.rule_id,
                probability=v.probability,
                penalty_amount=v.penalty_amount,
                expected_penalty_amount=v.expected_penalty_amount,
            )
            for v in result.violations
        ],
        total_expected_penalty_amount=result.total_expected_penalty_amount,
        stacking_mode=result.stacking_mode,
    )


def _try_get_summary_job(
    service: ProjectionSummaryService,
    purchase_order_id: UUID,
    as_of_date: date | None,
):
    """Resolve the cached summary job for one (PO, date), or `None` if there is nothing to show."""
    try:
        return service.get_status(purchase_order_id, as_of_date=as_of_date)
    except AppError:
        return None


class _SummaryAttachable(Protocol):
    """Response shape the `_attach_*` helpers need, shared by the detail and run-result models."""

    projection_date: date
    summary_status: SummaryStatus | None
    summary: PenaltyProjectionSummaryResponse | None
    mitigations: list[MitigationOptionResponse] | None
    mitigation_summary_status: SummaryStatus | None
    mitigation_summary: PenaltyMitigationSummaryResponse | None


def _attach_summary(
    row: _SummaryAttachable,
    service: ProjectionSummaryService,
    purchase_order_id: UUID,
) -> None:
    """Attach a projection summary to a response row if one exists and is ready."""
    job = _try_get_summary_job(service, purchase_order_id, row.projection_date)
    if job is None:
        return
    row.summary_status = SummaryStatus(job.status)
    if job.status == SummaryStatus.READY and job.output is not None:
        row.summary = PenaltyProjectionSummaryResponse.model_validate(job.output)


def _try_get_mitigation_summary_job(
    service: MitigationSummaryService,
    purchase_order_id: UUID,
    as_of_date: date | None,
):
    """Resolve the cached mitigation summary job for one (PO, date), or `None` if there is none."""
    try:
        return service.get_status(purchase_order_id, as_of_date=as_of_date)
    except AppError:
        return None


def _attach_mitigations(
    row: _SummaryAttachable,
    mitigation_options: MitigationOptionRepository,
    purchase_order_id: UUID,
) -> None:
    """Attach a row's mitigation options for its projection date, when any have been computed."""
    options = mitigation_options.list_for_date(purchase_order_id, row.projection_date)
    if options:
        row.mitigations = [MitigationOptionResponse.model_validate(o) for o in options]


def _attach_mitigation_summary(
    row: _SummaryAttachable,
    service: MitigationSummaryService,
    purchase_order_id: UUID,
) -> None:
    """Attach a mitigation summary to a response row if one exists and is ready."""
    job = _try_get_mitigation_summary_job(service, purchase_order_id, row.projection_date)
    if job is None:
        return
    row.mitigation_summary_status = SummaryStatus(job.status)
    if job.status == SummaryStatus.READY and job.output is not None:
        row.mitigation_summary = PenaltyMitigationSummaryResponse.model_validate(job.output)


@router.post(
    "/projections",
    response_model=Envelope[PenaltyProjectionResultResponse],
    status_code=status.HTTP_201_CREATED,
)
def run_penalty_projection(
    body: PenaltyProjectionRunRequest,
    projection_service: ProjectionService = Depends(get_projection_service),
) -> Envelope[PenaltyProjectionResultResponse]:
    """Compute and persist a penalty projection for a purchase order."""
    result = projection_service.run_for_purchase_order(
        body.purchase_order_id, body.projection_date, body.stacking_mode_override
    )
    response = _to_result_response(result)
    return success_envelope(response, message="Penalty projection computed.")


@router.get(
    "/projections",
    response_model=Envelope[list[PenaltyProjectionDetailResponse]],
)
def list_penalty_projections(
    purchase_order_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    projection_date: date | None = Query(default=None),
    projection_date_from: date | None = Query(default=None),
    projection_date_to: date | None = Query(default=None),
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    projections: PenaltyProjectionRepository = Depends(get_penalty_projection_repository),
    projection_summary_service: ProjectionSummaryService = Depends(get_projection_summary_service),
    mitigation_options: MitigationOptionRepository = Depends(get_mitigation_option_repository),
    mitigation_summary_service: MitigationSummaryService = Depends(get_mitigation_summary_service),
    include: set[str] = Depends(_INCLUDE_PROJECTION),
) -> Envelope[list[PenaltyProjectionDetailResponse]]:
    """List penalty projections, filtered by purchase order, status, or projection date.

    Omitting `purchase_order_id` lists across every purchase order and defaults `status` to `OPEN`.
    """
    if purchase_order_id is not None:
        purchase_orders.require_purchase_order(purchase_order_id)
        resolved_status = status.upper() if status is not None else None
    else:
        resolved_status = status.upper() if status is not None else "OPEN"

    rows = [
        PenaltyProjectionDetailResponse.model_validate(r)
        for r in projections.list_projections(
            purchase_order_id=purchase_order_id,
            status=resolved_status,
            projection_date=projection_date,
            projection_date_from=projection_date_from,
            projection_date_to=projection_date_to,
        )
    ]
    if "summary" in include:
        for row in rows:
            _attach_summary(row, projection_summary_service, row.purchase_order_id)
    if "mitigations" in include:
        for row in rows:
            _attach_mitigations(row, mitigation_options, row.purchase_order_id)
    if "mitigation_summary" in include:
        for row in rows:
            _attach_mitigation_summary(row, mitigation_summary_service, row.purchase_order_id)
    return success_envelope(rows)


@router.post(
    "/projections/summary",
    response_model=Envelope[PenaltyProjectionSummaryStatusResponse],
    responses={202: {"model": Envelope[PenaltyProjectionSummaryStatusResponse]}},
)
def trigger_penalty_projection_summary(
    body: PenaltyProjectionSummaryRequest,
    response: Response,
    projection_summary_service: ProjectionSummaryService = Depends(get_projection_summary_service),
) -> Envelope[PenaltyProjectionSummaryStatusResponse]:
    """Request or retrieve a summary of a projection, returning 202 if queued."""
    job = projection_summary_service.get_or_schedule(
        body.purchase_order_id, as_of_date=body.as_of_date, force_regenerate=body.force_regenerate
    )

    if job.status == SummaryStatus.READY:
        assert job.output is not None
        return success_envelope(
            PenaltyProjectionSummaryStatusResponse(
                purchase_order_id=body.purchase_order_id,
                as_of_date=job.as_of_date,
                status=SummaryStatus.READY,
                summary=PenaltyProjectionSummaryResponse.model_validate(job.output),
            )
        )

    # PENDING: get_or_schedule has already durably enqueued (and committed) a
    # process.job_run/job_item. Poll GET /penalties/projections/summary or
    # GET /penalties/projections/{projection_id}?include=summary for the result.
    response.status_code = 202
    return success_envelope(
        PenaltyProjectionSummaryStatusResponse(
            purchase_order_id=body.purchase_order_id,
            as_of_date=job.as_of_date,
            status=SummaryStatus.PENDING,
        ),
        message="Penalty projection summary generation queued.",
    )


@router.get(
    "/projections/summary",
    response_model=Envelope[PenaltyProjectionSummaryStatusResponse],
)
def get_penalty_projection_summary(
    purchase_order_id: UUID = Query(...),
    as_of_date: date | None = Query(default=None),
    projection_summary_service: ProjectionSummaryService = Depends(get_projection_summary_service),
) -> Envelope[PenaltyProjectionSummaryStatusResponse]:
    """Poll a projection summary job without refetching its projection; never schedules one.

    A purchase order with no summary job on record succeeds with a null status rather than 404ing.
    """
    try:
        job = projection_summary_service.get_status(purchase_order_id, as_of_date=as_of_date)
    except NotFoundError as exc:
        if exc.code != "NO_PROJECTION_SUMMARY_JOB_EXISTS":
            raise
        return success_envelope(
            PenaltyProjectionSummaryStatusResponse(
                purchase_order_id=purchase_order_id,
                as_of_date=as_of_date,
                status=None,
            )
        )
    return success_envelope(
        PenaltyProjectionSummaryStatusResponse(
            purchase_order_id=purchase_order_id,
            as_of_date=job.as_of_date,
            status=SummaryStatus(job.status),
            summary=(
                PenaltyProjectionSummaryResponse.model_validate(job.output)
                if job.status == SummaryStatus.READY and job.output is not None
                else None
            ),
        )
    )


# NOTE: `GET /penalties/projections/{projection_id}` MUST stay registered after
# the two literal `/penalties/projections/summary` routes above. Starlette
# matches in registration order, and `{projection_id}` greedily matches the
# literal `summary` segment too, which would turn that route into a 422
# UUID-parse failure.
@router.get(
    "/projections/{projection_id}",
    response_model=Envelope[PenaltyProjectionDetailResponse],
)
def get_penalty_projection(
    projection_id: UUID,
    projections: PenaltyProjectionRepository = Depends(get_penalty_projection_repository),
    projection_summary_service: ProjectionSummaryService = Depends(get_projection_summary_service),
    mitigation_options: MitigationOptionRepository = Depends(get_mitigation_option_repository),
    mitigation_summary_service: MitigationSummaryService = Depends(get_mitigation_summary_service),
    include: set[str] = Depends(_INCLUDE_PROJECTION),
) -> Envelope[PenaltyProjectionDetailResponse]:
    """Return a penalty projection without triggering summary generation."""
    row = projections.get_by_id(projection_id)
    if row is None:
        raise NotFoundError(
            code="PROJECTION_NOT_FOUND",
            message=f"No penalty projection found with projection_id={projection_id}",
        )
    detail = PenaltyProjectionDetailResponse.model_validate(row)
    if "summary" in include:
        _attach_summary(detail, projection_summary_service, row["purchase_order_id"])
    if "mitigations" in include:
        _attach_mitigations(detail, mitigation_options, row["purchase_order_id"])
    if "mitigation_summary" in include:
        _attach_mitigation_summary(detail, mitigation_summary_service, row["purchase_order_id"])
    return success_envelope(detail)


@router.get(
    "/exposure",
    response_model=Envelope[PenaltyExposureResponse],
)
def get_penalty_exposure(
    purchase_order_id: UUID = Query(...),
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    projections: PenaltyProjectionRepository = Depends(get_penalty_projection_repository),
) -> Envelope[PenaltyExposureResponse]:
    """Retrieve the latest penalty exposure (the most recent projection) for a purchase order."""
    purchase_orders.require_purchase_order(purchase_order_id)
    latest = projections.get_latest(purchase_order_id)
    if latest is None:
        raise NotFoundError(
            code="NO_PROJECTION_EXISTS",
            message=f"No projections exist yet for purchase_order_id={purchase_order_id}",
        )
    return success_envelope(PenaltyExposureResponse.model_validate(latest))
