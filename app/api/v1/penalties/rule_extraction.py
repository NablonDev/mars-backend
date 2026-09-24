"""API endpoints for penalty rule extraction, reviewer revisions, and publication.

Non-LLM rule-extraction endpoints (retailer agreement upload/list/get, extraction status,
extraction runs, extracted-rule reads, review, revision listing, publication audit) moved
to mars-bff; see `docs/API.md` "Retailer agreements and rule extraction" for their new
paths. Only routes that run an agent/LLM stay here, plus `publish` (the deterministic
rule compiler, an engine-work exception to that split).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, status

from app.api.dependencies import get_database, get_penalty_rule_extraction_service, get_rule_revision_service
from app.core.container import Container
from app.core.envelope import Envelope, success_envelope
from app.db.session import Database
from app.schemas.penalties.rule_extraction import (
    ExtractionStartResponse,
    RulePublicationOutcomeResponse,
    RulePublicationResultResponse,
    RuleRevisionRequest,
    RuleRevisionResponse,
)
from app.services.penalties.rule_extraction.revision import run_rule_revision

if TYPE_CHECKING:
    from app.services.penalties.rule_extraction.revision import RuleRevisionService
    from app.services.penalties.rule_extraction.service import PenaltyRuleExtractionService

router = APIRouter(prefix="/penalties", tags=["penalty-rule-extraction"])


@router.post(
    "/retailer-agreements/{retailer_agreement_id}/extract",
    response_model=Envelope[ExtractionStartResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
def start_extraction(
    retailer_agreement_id: UUID,
    service: PenaltyRuleExtractionService = Depends(get_penalty_rule_extraction_service),
) -> Envelope[ExtractionStartResponse]:
    """Start the rule extraction graph for a retailer agreement, returning its run id and staged count."""
    result = service.start_extraction(retailer_agreement_id)
    return success_envelope(
        ExtractionStartResponse(
            retailer_agreement_id=retailer_agreement_id,
            agent_run_id=result.agent_run_id,
            staged_count=result.staged_count,
        ),
        message="Penalty rule extraction started.",
    )


@router.post(
    "/retailer-agreements/{retailer_agreement_id}/extracted-rules/{extracted_rule_id}/revisions",
    response_model=Envelope[RuleRevisionResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
def request_rule_revision(
    retailer_agreement_id: UUID,
    extracted_rule_id: UUID,
    body: RuleRevisionRequest,
    background_tasks: BackgroundTasks,
    revision_service: RuleRevisionService = Depends(get_rule_revision_service),
    database: Database = Depends(get_database),
) -> Envelope[RuleRevisionResponse]:
    """Queue a reviewer's revision instruction for one staged rule and run it in the background."""
    revision = revision_service.request_revision(
        retailer_agreement_id=retailer_agreement_id,
        extracted_rule_id=extracted_rule_id,
        instruction=body.instruction,
        requested_by=body.requested_by,
    )
    container = Container.build()
    background_tasks.add_task(
        run_rule_revision,
        revision["id"],
        database=database,
        classify=container.rule_extraction_classify,
        extract_facts=container.rule_extraction_extract_facts,
    )
    return success_envelope(RuleRevisionResponse.model_validate(revision), message="Rule revision queued.")


@router.post(
    "/retailer-agreements/{retailer_agreement_id}/publish",
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
