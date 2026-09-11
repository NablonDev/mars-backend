"""API endpoints for contract upload, penalty rule extraction, review, and publication."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict

from app.api.dependencies import (
    get_contract_repository,
    get_penalty_rule_extraction_service,
    get_rule_publication_repository,
)
from app.core.envelope import Envelope, success_envelope
from app.core.exceptions import NotFoundError
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import RulePublicationRepository
from app.schemas.penalties.rule_extraction import (
    ContractCreateRequest,
    ContractResponse,
    ExtractedPenaltyRuleResponse,
    ExtractedPenaltyRuleReviewRequest,
    ExtractionStartResponse,
    RulePublicationOutcomeResponse,
    RulePublicationResultResponse,
)

if TYPE_CHECKING:
    from app.services.penalties.rule_extraction.service import PenaltyRuleExtractionService

router = APIRouter(tags=["penalty-rule-extraction"])


class PublicationAuditResponse(BaseModel):
    """Every recorded publication outcome for a contract plus a rejection-reason histogram."""

    model_config = ConfigDict(from_attributes=True)

    contract_id: UUID
    outcomes: list[RulePublicationOutcomeResponse]
    rejection_reason_histogram: dict[str, int]


def _require_contract(contracts: RetailerAgreementRepository, contract_id: UUID):
    """Fetch a contract by id, raising `NotFoundError` if it doesn't exist."""
    contract = contracts.get(contract_id)
    if contract is None:
        raise NotFoundError(
            code="CONTRACT_NOT_FOUND", message=f"No contract found with contract_id={contract_id}"
        )
    return contract


@router.post(
    "/penalties/contracts", response_model=Envelope[ContractResponse], status_code=status.HTTP_201_CREATED
)
def create_contract(
    body: ContractCreateRequest,
    response: Response,
    contracts: RetailerAgreementRepository = Depends(get_contract_repository),
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[ContractResponse]:
    """Create a contract, or return the existing one if its content already matches by sha256."""
    document_sha256 = hashlib.sha256(body.markdown_text.encode("utf-8")).hexdigest()
    existing = contracts.get_by_sha256(document_sha256)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return success_envelope(ContractResponse.model_validate(existing), message="Contract already exists.")

    created = service.create_contract(
        retailer_id=body.retailer_id,
        contract_code=body.contract_code,
        title=body.title,
        markdown_text=body.markdown_text,
        source_uri=body.source_uri,
        effective_date=body.effective_date,
        expiration_date=body.expiration_date,
    )
    return success_envelope(ContractResponse.model_validate(created), message="Contract created.")


@router.get("/penalties/contracts/{contract_id}", response_model=Envelope[ContractResponse])
def get_contract(
    contract_id: UUID,
    contracts: RetailerAgreementRepository = Depends(get_contract_repository),
) -> Envelope[ContractResponse]:
    """Fetch one contract by id."""
    contract = _require_contract(contracts, contract_id)
    return success_envelope(ContractResponse.model_validate(contract))


@router.post(
    "/penalties/contracts/{contract_id}/extract",
    response_model=Envelope[ExtractionStartResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
def start_extraction(
    contract_id: UUID,
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[ExtractionStartResponse]:
    """Start the rule extraction graph for a contract, returning its run and thread ids."""
    result = service.start_extraction(contract_id)
    return success_envelope(
        ExtractionStartResponse(
            contract_id=contract_id,
            agent_run_id=result.agent_run_id,
            workflow_thread_id=result.thread_id,
        ),
        message="Penalty rule extraction started.",
    )


@router.get(
    "/penalties/contracts/{contract_id}/extracted-rules",
    response_model=Envelope[list[ExtractedPenaltyRuleResponse]],
)
def list_extracted_rules(
    contract_id: UUID,
    status: Literal["PENDING_REVIEW", "APPROVED", "REJECTED"] | None = Query(default=None),
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[list[ExtractedPenaltyRuleResponse]]:
    """List a contract's extracted rules, optionally narrowed by review status."""
    rows = [
        ExtractedPenaltyRuleResponse.model_validate(r)
        for r in service.list_extracted_rules(contract_id, status=status)
    ]
    return success_envelope(rows)


@router.post(
    "/penalties/contracts/{contract_id}/extracted-rules/{extracted_rule_id}/review",
    response_model=Envelope[ExtractedPenaltyRuleResponse],
)
def review_extracted_rule(
    contract_id: UUID,
    extracted_rule_id: UUID,
    body: ExtractedPenaltyRuleReviewRequest,
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[ExtractedPenaltyRuleResponse]:
    """Record a reviewer's APPROVED or REJECTED decision on one extracted rule."""
    reviewed = service.submit_review(
        contract_id, extracted_rule_id, body.status, review_notes=body.review_notes
    )
    return success_envelope(
        ExtractedPenaltyRuleResponse.model_validate(reviewed), message="Extracted rule reviewed."
    )


@router.post(
    "/penalties/contracts/{contract_id}/publish", response_model=Envelope[RulePublicationResultResponse]
)
def publish_contract_rules(
    contract_id: UUID,
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[RulePublicationResultResponse]:
    """Publish a contract's approved extracted rules into live penalty rules."""
    result = service.publish(contract_id)
    return success_envelope(
        RulePublicationResultResponse(
            contract_id=contract_id,
            agent_run_id=result.agent_run_id,
            published_count=result.published_count,
            rejected_count=result.rejected_count,
            outcomes=[RulePublicationOutcomeResponse.model_validate(o) for o in result.outcomes],
        ),
        message="Penalty rule publication complete.",
    )


@router.get(
    "/penalties/contracts/{contract_id}/publications", response_model=Envelope[PublicationAuditResponse]
)
def get_publication_audit(
    contract_id: UUID,
    contracts: RetailerAgreementRepository = Depends(get_contract_repository),
    publications: RulePublicationRepository = Depends(get_rule_publication_repository),
) -> Envelope[PublicationAuditResponse]:
    """Return every publication outcome for a contract plus its rejection-reason histogram."""
    _require_contract(contracts, contract_id)
    outcomes = [
        RulePublicationOutcomeResponse.model_validate(o) for o in publications.list_for_contract(contract_id)
    ]
    return success_envelope(
        PublicationAuditResponse(
            contract_id=contract_id,
            outcomes=outcomes,
            rejection_reason_histogram=publications.reason_histogram(contract_id),
        )
    )
