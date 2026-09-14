"""API endpoints for retailer agreement upload, penalty rule extraction, review, and publication."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.dependencies import (
    get_penalty_rule_extraction_service,
    get_retailer_agreement_repository,
    get_rule_publication_repository,
)
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import NotFoundError
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import RulePublicationRepository
from app.schemas.penalties.rule_extraction import (
    ExtractedPenaltyRuleResponse,
    ExtractedPenaltyRuleReviewRequest,
    ExtractionStartResponse,
    ExtractionStatusResponse,
    PublicationAuditResponse,
    RetailerAgreementCreateRequest,
    RetailerAgreementResponse,
    RulePublicationOutcomeResponse,
    RulePublicationResultResponse,
)
from app.utils.hashing import content_sha256

if TYPE_CHECKING:
    from app.services.penalties.rule_extraction.service import PenaltyRuleExtractionService

router = APIRouter(tags=["penalty-rule-extraction"])


def _require_retailer_agreement(
    retailer_agreements: RetailerAgreementRepository, retailer_agreement_id: UUID
):
    """Fetch a retailer agreement by id, raising `NotFoundError` if it doesn't exist."""
    retailer_agreement = retailer_agreements.get(retailer_agreement_id)
    if retailer_agreement is None:
        raise NotFoundError(
            code="RETAILER_AGREEMENT_NOT_FOUND",
            message=f"No retailer agreement found with retailer_agreement_id={retailer_agreement_id}",
        )
    return retailer_agreement


@router.post(
    "/penalties/retailer-agreements",
    response_model=Envelope[RetailerAgreementResponse],
    status_code=status.HTTP_201_CREATED,
)
def create_retailer_agreement(
    body: RetailerAgreementCreateRequest,
    response: Response,
    retailer_agreements: RetailerAgreementRepository = Depends(get_retailer_agreement_repository),
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[RetailerAgreementResponse]:
    """Create a retailer agreement, or return the existing one if its content already matches by sha256."""
    document_sha256 = content_sha256(body.markdown_text)
    existing = retailer_agreements.get_by_sha256(document_sha256)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return success_envelope(
            RetailerAgreementResponse.model_validate(existing), message="Retailer agreement already exists."
        )

    created = service.create_retailer_agreement(
        retailer_id=body.retailer_id,
        contract_code=body.contract_code,
        title=body.title,
        markdown_text=body.markdown_text,
        source_uri=body.source_uri,
        effective_date=body.effective_date,
        expiration_date=body.expiration_date,
    )
    return success_envelope(
        RetailerAgreementResponse.model_validate(created), message="Retailer agreement created."
    )


@router.get(
    "/penalties/retailer-agreements",
    response_model=Envelope[list[RetailerAgreementResponse]],
)
def list_retailer_agreements(
    retailer_agreements: RetailerAgreementRepository = Depends(get_retailer_agreement_repository),
) -> Envelope[list[RetailerAgreementResponse]]:
    """List every retailer agreement, newest first.

    No `status` filter yet: `retailer_agreement` has no `status` column today, so one
    isn't offered here rather than silently ignored; adding it needs its own schema
    decision (see docs/API.md).
    """
    rows = [RetailerAgreementResponse.model_validate(r) for r in retailer_agreements.list_all()]
    return success_envelope(rows)


@router.get(
    "/penalties/retailer-agreements/{retailer_agreement_id}",
    response_model=Envelope[RetailerAgreementResponse],
)
def get_retailer_agreement(
    retailer_agreement_id: UUID,
    retailer_agreements: RetailerAgreementRepository = Depends(get_retailer_agreement_repository),
) -> Envelope[RetailerAgreementResponse]:
    """Fetch one retailer agreement by id."""
    retailer_agreement = _require_retailer_agreement(retailer_agreements, retailer_agreement_id)
    return success_envelope(RetailerAgreementResponse.model_validate(retailer_agreement))


@router.post(
    "/penalties/retailer-agreements/{retailer_agreement_id}/extract",
    response_model=Envelope[ExtractionStartResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
def start_extraction(
    retailer_agreement_id: UUID,
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[ExtractionStartResponse]:
    """Start the rule extraction graph for a retailer agreement, returning its run and thread ids."""
    result = service.start_extraction(retailer_agreement_id)
    return success_envelope(
        ExtractionStartResponse(
            retailer_agreement_id=retailer_agreement_id,
            agent_run_id=result.agent_run_id,
            workflow_thread_id=result.thread_id,
        ),
        message="Penalty rule extraction started.",
    )


@router.get(
    "/penalties/retailer-agreements/{retailer_agreement_id}/extraction",
    response_model=Envelope[ExtractionStatusResponse],
)
def get_extraction_status(
    retailer_agreement_id: UUID,
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[ExtractionStatusResponse]:
    """Return the most recent extraction run's status for a retailer agreement."""
    result = service.get_extraction_status(retailer_agreement_id)
    return success_envelope(ExtractionStatusResponse.model_validate(result))


@router.get(
    "/penalties/retailer-agreements/{retailer_agreement_id}/extracted-rules",
    response_model=Envelope[list[ExtractedPenaltyRuleResponse]],
)
def list_extracted_rules(
    retailer_agreement_id: UUID,
    status: Literal["PENDING_REVIEW", "APPROVED", "REJECTED"] | None = Query(default=None),
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[list[ExtractedPenaltyRuleResponse]]:
    """List a retailer agreement's extracted rules, optionally narrowed by review status."""
    rows = [
        ExtractedPenaltyRuleResponse.model_validate(r)
        for r in service.list_extracted_rules(retailer_agreement_id, status=status)
    ]
    return success_envelope(rows)


@router.get(
    "/penalties/retailer-agreements/{retailer_agreement_id}/extracted-rules/{extracted_rule_id}",
    response_model=Envelope[ExtractedPenaltyRuleResponse],
)
def get_extracted_rule(
    retailer_agreement_id: UUID,
    extracted_rule_id: UUID,
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[ExtractedPenaltyRuleResponse]:
    """Fetch one extracted rule with its attributes."""
    result = service.get_extracted_rule(retailer_agreement_id, extracted_rule_id)
    return success_envelope(ExtractedPenaltyRuleResponse.model_validate(result))


@router.post(
    "/penalties/retailer-agreements/{retailer_agreement_id}/extracted-rules/{extracted_rule_id}/review",
    response_model=Envelope[ExtractedPenaltyRuleResponse],
)
def review_extracted_rule(
    retailer_agreement_id: UUID,
    extracted_rule_id: UUID,
    body: ExtractedPenaltyRuleReviewRequest,
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[ExtractedPenaltyRuleResponse]:
    """Record a reviewer's APPROVED or REJECTED decision on one extracted rule."""
    reviewed = service.submit_review(
        retailer_agreement_id, extracted_rule_id, body.status, review_notes=body.review_notes
    )
    return success_envelope(
        ExtractedPenaltyRuleResponse.model_validate(reviewed), message="Extracted rule reviewed."
    )


@router.post(
    "/penalties/retailer-agreements/{retailer_agreement_id}/publish",
    response_model=Envelope[RulePublicationResultResponse],
)
def publish_retailer_agreement_rules(
    retailer_agreement_id: UUID,
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[RulePublicationResultResponse]:
    """Publish a retailer agreement's approved extracted rules into live penalty rules."""
    result = service.publish(retailer_agreement_id)
    return success_envelope(
        RulePublicationResultResponse(
            retailer_agreement_id=retailer_agreement_id,
            agent_run_id=result.agent_run_id,
            published_count=result.published_count,
            rejected_count=result.rejected_count,
            outcomes=[RulePublicationOutcomeResponse.model_validate(o) for o in result.outcomes],
        ),
        message="Penalty rule publication complete.",
    )


@router.get(
    "/penalties/retailer-agreements/{retailer_agreement_id}/publications",
    response_model=Envelope[PublicationAuditResponse],
)
def get_publication_audit(
    retailer_agreement_id: UUID,
    retailer_agreements: RetailerAgreementRepository = Depends(get_retailer_agreement_repository),
    publications: RulePublicationRepository = Depends(get_rule_publication_repository),
) -> Envelope[PublicationAuditResponse]:
    """Return every publication outcome for a retailer agreement plus its rejection-reason histogram."""
    _require_retailer_agreement(retailer_agreements, retailer_agreement_id)
    outcomes = [
        RulePublicationOutcomeResponse.model_validate(o)
        for o in publications.list_for_retailer_agreement(retailer_agreement_id)
    ]
    return success_envelope(
        PublicationAuditResponse(
            retailer_agreement_id=retailer_agreement_id,
            outcomes=outcomes,
            rejection_reason_histogram=publications.reason_histogram(retailer_agreement_id),
        )
    )
