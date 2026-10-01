"""FastAPI dependency factories for database, repository, service, and queue
components.

Two composition styles coexist (approved plan, Phase 7a):

- `common`/`penalties`: a session-based `Depends()` chain
  (`get_database -> get_session -> get_<x>_repository -> get_<x>_service`),
  rebuilt here against the Phase 2/3 repositories and services -- this
  module was stale from before that restructure (importing repository/
  service classes that no longer exist) until this pass.
- `cmir`/`po_validation`: the container-based composition in
  `app/core/container.py` -- a separate composition root, since the two
  LangGraph graphs share one process-lifetime `PostgresSaver` checkpointer.
  `build_service`/`build_po_validation_service` below just hand
  `CmirService`/`PoValidationService` a reference to `Container`'s
  `cmir_repos`/`cmir_unit_of_work` (resp. `po_validation_repos`/
  `po_validation_unit_of_work`) factory methods -- neither service holds a
  repository or compiled graph on `self` any more (session-lifecycle fix:
  each public method opens its own fresh Session per invocation via one of
  those factories, committed on success/rolled back on exception/closed
  either way). `Container` now also builds the `process.job_queue`/
  `cmir.cmir_job_*_context` repositories `CmirService` needs, as part of
  `cmir_repos`, instead of this module opening a second, separate session
  for them.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from rdflib import Graph as RdfGraph
from sqlalchemy.orm import Session

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import LLMConfig, Settings, get_settings
from app.core.container import Container
from app.core.exceptions import ValidationError
from app.db.session import Database
from app.queue.interfaces import JobDispatcher, JobSource
from app.repositories.common.delivery_change_request import PoDeliveryChangeRequestRepository
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.dispute import PenaltyDisputeRepository
from app.repositories.penalties.fulfillment_risk import FulfillmentRiskRepository
from app.repositories.penalties.job_context import (
    PenaltyJobItemContextRepository,
    PenaltyJobRunContextRepository,
)
from app.repositories.penalties.mitigation import MitigationInputRepository, MitigationOptionRepository
from app.repositories.penalties.projection import ActualPenaltyRepository, PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.rule_extraction import (
    ExtractedPenaltyRuleRepository,
    ExtractedPenaltyRuleRevisionRepository,
    RulePublicationRepository,
)
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.penalties.timeline_alert import TimelineAlertRepository
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.repositories.process.workflow import HumanActionRepository, WorkflowThreadRepository
from app.services.cmir.service import CmirService
from app.services.ontology.context_service import OntologyContextService
from app.services.ontology.graph_service import OntologyGraphService
from app.services.ontology_insert.run_service import OntologyInsertRunService
from app.services.ontology_update.run_service import OntologyUpdateRunService
from app.services.penalties.delivery_change import PoDeliveryChangeRequestService
from app.services.penalties.dispute.service import DisputeResolutionService
from app.services.penalties.dispute.summary_service import DisputeSummaryService
from app.services.penalties.mitigation.service import MitigationService
from app.services.penalties.mitigation.summary_service import MitigationSummaryService
from app.services.penalties.projection.service import ProjectionService
from app.services.penalties.projection.summary_service import ProjectionSummaryService
from app.services.penalties.rule_extraction.revision import RuleRevisionService
from app.services.penalties.rule_extraction.service import PenaltyRuleExtractionService
from app.services.penalties.timeline.service import TimelineProjectionService
from app.services.po_validation.service import PoValidationService
from app.services.seeding.service import PenaltySeedingService

logger = logging.getLogger(__name__)


def require_internal_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_internal_api_key: Annotated[str | None, Header(alias="X-Internal-Api-Key")] = None,
) -> None:
    """Gate every non-health route behind a shared-secret header.

    Compares `X-Internal-Api-Key` against the configured secret using a
    constant-time comparison and raises 401 if the header is missing,
    non-Latin-1, or doesn't match.
    """
    valid = False
    if x_internal_api_key is not None:
        try:
            valid = secrets.compare_digest(
                x_internal_api_key.encode("latin-1"),
                settings.app.internal_api_key.encode("latin-1"),
            )
        except UnicodeEncodeError:
            valid = False
    if not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def parse_include(allowed: frozenset[str]):
    """Return a dependency that validates `?include=` query params against an allow-list."""

    def _dependency(include: Annotated[str | None, Query()] = None) -> set[str]:
        """Parse and validate the `?include=` query parameter against the allowed list.

        Splits comma-separated tokens and validates each against the allowed set,
        raising ValidationError if any unknown tokens are found.
        """
        if not include:
            return set()

        requested = {token.strip() for token in include.split(",") if token.strip()}
        unknown = requested - allowed
        if unknown:
            raise ValidationError(
                code="INVALID_INCLUDE",
                message=(f"Unsupported include value(s): {sorted(unknown)}. Allowed: {sorted(allowed)}."),
                details={"unknown": sorted(unknown), "allowed": sorted(allowed)},
            )
        return requested

    return _dependency


def get_database(request: Request) -> Database:
    """Return the process-wide Database created during application startup."""
    return request.app.state.database


def get_session(database: Annotated[Database, Depends(get_database)]) -> Generator[Session, None, None]:
    """Provide a scoped database session, committed/rolled-back/closed by `database.session()`.

    Note:
        Call sites should pass `scope="function"` to `Depends(get_session)` to ensure
        the session is committed and returned to the pool *before* the response is sent,
        preventing race conditions on immediate read-after-write operations.
    """
    with database.session() as session:
        yield session


# ---------------------------------------------------------------------------
# common: repositories
# ---------------------------------------------------------------------------


def get_master_data_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> MasterDataRepository:
    """Provide a master data repository for reading carrier, plant, and SKU data."""
    return MasterDataRepository(session)


def get_purchase_order_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> PurchaseOrderRepository:
    """Provide a purchase order repository for reading and writing PO headers and lines."""
    return PurchaseOrderRepository(session)


def get_fulfillment_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> FulfillmentRepository:
    """Provide a fulfillment repository for reading and writing shipments and demand exceptions."""
    return FulfillmentRepository(session)


def get_fulfillment_timeline_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> FulfillmentTimelineRepository:
    """Provide a fulfillment-timeline repository for plans, milestones, events, and upstream supply."""
    return FulfillmentTimelineRepository(session)


# ---------------------------------------------------------------------------
# process: repositories (shared backbone)
# ---------------------------------------------------------------------------


def get_job_queue_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> JobQueueRepository:
    """Provide a job queue repository for managing background job execution state."""
    return JobQueueRepository(session)


def get_agent_registry_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> AgentRegistryRepository:
    """Provide an agent registry repository for tracking agent state and runs."""
    return AgentRegistryRepository(session)


def get_agent_run_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> AgentRunRepository:
    """Provide an agent-run repository bound to the request session."""
    return AgentRunRepository(session)


# ---------------------------------------------------------------------------
# penalties: repositories
# ---------------------------------------------------------------------------


def get_penalty_rule_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> PenaltyRuleRepository:
    """Provide a penalty rule repository for accessing rule configurations."""
    return PenaltyRuleRepository(session)


def get_penalty_projection_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> PenaltyProjectionRepository:
    """Provide a penalty projection repository for reading and writing projections."""
    return PenaltyProjectionRepository(session)


def get_actual_penalty_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> ActualPenaltyRepository:
    """Provide an actual penalty repository for reading and writing realized penalties."""
    return ActualPenaltyRepository(session)


def get_fulfillment_risk_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> FulfillmentRiskRepository:
    """Provide a fulfillment-risk repository for reading and writing timeline risks/mitigation options."""
    return FulfillmentRiskRepository(session)


def get_timeline_alert_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> TimelineAlertRepository:
    """Provide a timeline-alert repository for the ops-tracked fulfillment-timeline alert lifecycle."""
    return TimelineAlertRepository(session)


def get_penalty_summary_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> PenaltySummaryRepository:
    """Provide a penalty summary repository for accessing summary job data."""
    return PenaltySummaryRepository(session)


def get_mitigation_input_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> MitigationInputRepository:
    """Provide a mitigation input repository for reading mitigation configuration."""
    return MitigationInputRepository(session)


def get_mitigation_option_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> MitigationOptionRepository:
    """Provide a mitigation option repository for reading and writing computed options."""
    return MitigationOptionRepository(session)


def get_dispute_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> PenaltyDisputeRepository:
    """Provide a dispute repository for reading and writing penalty disputes."""
    return PenaltyDisputeRepository(session)


def get_delivery_change_request_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> PoDeliveryChangeRequestRepository:
    """Provide a delivery change request repository for tracking PO delivery modifications."""
    return PoDeliveryChangeRequestRepository(session)


def get_penalty_job_item_context_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> PenaltyJobItemContextRepository:
    """Provide a penalty job item context repository for job execution details."""
    return PenaltyJobItemContextRepository(session)


def get_penalty_job_run_context_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> PenaltyJobRunContextRepository:
    """Provide a penalty job run context repository for top-level job state."""
    return PenaltyJobRunContextRepository(session)


def get_retailer_agreement_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> RetailerAgreementRepository:
    """Provide a retailer agreement repository for reading and writing retailer agreement documents."""
    return RetailerAgreementRepository(session)


def get_extracted_penalty_rule_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> ExtractedPenaltyRuleRepository:
    """Provide an extracted penalty rule repository for the review-and-publication staging area."""
    return ExtractedPenaltyRuleRepository(session)


def get_rule_publication_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> RulePublicationRepository:
    """Provide a rule publication repository for the append-only publication audit trail."""
    return RulePublicationRepository(session)


def get_extracted_penalty_rule_revision_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> ExtractedPenaltyRuleRevisionRepository:
    """Provide an extracted penalty rule revision repository for the reviewer revision audit trail."""
    return ExtractedPenaltyRuleRevisionRepository(session)


def get_workflow_thread_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> WorkflowThreadRepository:
    """Provide a workflow thread repository bound to the request session."""
    return WorkflowThreadRepository(session)


def get_human_action_repository(
    session: Annotated[Session, Depends(get_session, scope="function")],
) -> HumanActionRepository:
    """Provide a human action repository bound to the request session."""
    return HumanActionRepository(session)


# ---------------------------------------------------------------------------
# job queue dispatch: POST /job-runs only. Summary generation enqueues and
# commits its own job_run/job_item internally (see ProjectionSummaryService/
# MitigationSummaryService.get_or_schedule), so those routes need no separate
# dispatch wiring.
# ---------------------------------------------------------------------------


def get_job_queue(request: Request) -> tuple[JobDispatcher, JobSource]:
    """Return the application-scoped job dispatcher and source."""
    return request.app.state.job_queue


def get_job_dispatcher(
    job_queue: Annotated[tuple[JobDispatcher, JobSource], Depends(get_job_queue)],
) -> JobDispatcher:
    """Provide the job dispatcher for enqueuing background work."""
    return job_queue[0]


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------


def get_llm_client_for_app(app: FastAPI, settings: Settings) -> AzureOpenAIChatClient:
    """Return the application-scoped LLM client, creating it on first use."""
    client = getattr(app.state, "llm_client", None)
    if client is None:
        config = LLMConfig.from_settings(settings)
        client = AzureOpenAIChatClient(
            config,
            max_retries=config.max_retries,
            timeout_seconds=config.timeout_seconds,
        )
        app.state.llm_client = client
    return client


def get_llm_client(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> AzureOpenAIChatClient:
    """Provide the application-scoped Azure OpenAI LLM client."""
    return get_llm_client_for_app(request.app, settings)


# ---------------------------------------------------------------------------
# penalties: services
# ---------------------------------------------------------------------------


def get_projection_service(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    fulfillment: Annotated[FulfillmentRepository, Depends(get_fulfillment_repository)],
    rules: Annotated[PenaltyRuleRepository, Depends(get_penalty_rule_repository)],
    master_data: Annotated[MasterDataRepository, Depends(get_master_data_repository)],
    projections: Annotated[PenaltyProjectionRepository, Depends(get_penalty_projection_repository)],
) -> ProjectionService:
    """Provide a penalty projection service for computing penalty exposure."""
    return ProjectionService(
        purchase_orders=purchase_orders,
        fulfillment=fulfillment,
        rules=rules,
        master_data=master_data,
        projections=projections,
    )


def get_timeline_projection_service(
    timeline: Annotated[FulfillmentTimelineRepository, Depends(get_fulfillment_timeline_repository)],
    risks: Annotated[FulfillmentRiskRepository, Depends(get_fulfillment_risk_repository)],
    alerts: Annotated[TimelineAlertRepository, Depends(get_timeline_alert_repository)],
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    rules: Annotated[PenaltyRuleRepository, Depends(get_penalty_rule_repository)],
    master_data: Annotated[MasterDataRepository, Depends(get_master_data_repository)],
) -> TimelineProjectionService:
    """Provide a fulfillment-timeline projection service for the event-driven penalty engine."""
    return TimelineProjectionService(
        timeline=timeline,
        risks=risks,
        alerts=alerts,
        purchase_orders=purchase_orders,
        rules=rules,
        master_data=master_data,
    )


def get_mitigation_service(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    rules: Annotated[PenaltyRuleRepository, Depends(get_penalty_rule_repository)],
    master_data: Annotated[MasterDataRepository, Depends(get_master_data_repository)],
    projections: Annotated[PenaltyProjectionRepository, Depends(get_penalty_projection_repository)],
    mitigation_inputs: Annotated[MitigationInputRepository, Depends(get_mitigation_input_repository)],
    mitigation_options: Annotated[MitigationOptionRepository, Depends(get_mitigation_option_repository)],
    projection_service: Annotated[ProjectionService, Depends(get_projection_service)],
) -> MitigationService:
    """Provide a mitigation service for computing penalty reduction options."""
    return MitigationService(
        purchase_orders=purchase_orders,
        rules=rules,
        master_data=master_data,
        projections=projections,
        mitigation_inputs=mitigation_inputs,
        mitigation_options=mitigation_options,
        projection_service=projection_service,
    )


def get_delivery_change_request_service(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    delivery_change_requests: Annotated[
        PoDeliveryChangeRequestRepository, Depends(get_delivery_change_request_repository)
    ],
    projection_service: Annotated[ProjectionService, Depends(get_projection_service)],
    master_data: Annotated[MasterDataRepository, Depends(get_master_data_repository)],
) -> PoDeliveryChangeRequestService:
    """Provide a delivery change request service for managing delivery date modifications."""
    return PoDeliveryChangeRequestService(
        purchase_orders=purchase_orders,
        delivery_change_requests=delivery_change_requests,
        projection_service=projection_service,
        master_data=master_data,
    )


def get_dispute_service(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    disputes: Annotated[PenaltyDisputeRepository, Depends(get_dispute_repository)],
    actual_penalties: Annotated[ActualPenaltyRepository, Depends(get_actual_penalty_repository)],
    rules: Annotated[PenaltyRuleRepository, Depends(get_penalty_rule_repository)],
    projection_service: Annotated[ProjectionService, Depends(get_projection_service)],
    fulfillment: Annotated[FulfillmentRepository, Depends(get_fulfillment_repository)],
    fulfillment_timeline: Annotated[
        FulfillmentTimelineRepository, Depends(get_fulfillment_timeline_repository)
    ],
    retailer_agreements: Annotated[RetailerAgreementRepository, Depends(get_retailer_agreement_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DisputeResolutionService:
    """Provide a dispute service for opening and managing penalty disputes."""
    return DisputeResolutionService(
        purchase_orders=purchase_orders,
        disputes=disputes,
        actual_penalties=actual_penalties,
        rules=rules,
        projection_service=projection_service,
        fulfillment=fulfillment,
        fulfillment_timeline=fulfillment_timeline,
        retailer_agreements=retailer_agreements,
        default_window_days=settings.dispute.default_window_days,
    )


def get_dispute_summary_service(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    summaries: Annotated[PenaltySummaryRepository, Depends(get_penalty_summary_repository)],
    agent_registry: Annotated[AgentRegistryRepository, Depends(get_agent_registry_repository)],
    job_queue: Annotated[JobQueueRepository, Depends(get_job_queue_repository)],
    job_context: Annotated[PenaltyJobItemContextRepository, Depends(get_penalty_job_item_context_repository)],
    llm: Annotated[AzureOpenAIChatClient, Depends(get_llm_client)],
    disputes: Annotated[PenaltyDisputeRepository, Depends(get_dispute_repository)],
    rules: Annotated[PenaltyRuleRepository, Depends(get_penalty_rule_repository)],
    master_data: Annotated[MasterDataRepository, Depends(get_master_data_repository)],
) -> DisputeSummaryService:
    """Provide a dispute summary service for generating LLM-powered dispute resolutions."""
    return DisputeSummaryService(
        purchase_orders=purchase_orders,
        summaries=summaries,
        agent_registry=agent_registry,
        job_queue=job_queue,
        job_context=job_context,
        llm=llm,
        disputes=disputes,
        rules=rules,
        master_data=master_data,
    )


def get_projection_summary_service(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    summaries: Annotated[PenaltySummaryRepository, Depends(get_penalty_summary_repository)],
    agent_registry: Annotated[AgentRegistryRepository, Depends(get_agent_registry_repository)],
    job_queue: Annotated[JobQueueRepository, Depends(get_job_queue_repository)],
    job_context: Annotated[PenaltyJobItemContextRepository, Depends(get_penalty_job_item_context_repository)],
    llm: Annotated[AzureOpenAIChatClient, Depends(get_llm_client)],
    rules: Annotated[PenaltyRuleRepository, Depends(get_penalty_rule_repository)],
    master_data: Annotated[MasterDataRepository, Depends(get_master_data_repository)],
    projections: Annotated[PenaltyProjectionRepository, Depends(get_penalty_projection_repository)],
    actual_penalties: Annotated[ActualPenaltyRepository, Depends(get_actual_penalty_repository)],
    projection_service: Annotated[ProjectionService, Depends(get_projection_service)],
) -> ProjectionSummaryService:
    """Provide a projection summary service for generating LLM-powered projection analyses."""
    return ProjectionSummaryService(
        purchase_orders=purchase_orders,
        summaries=summaries,
        agent_registry=agent_registry,
        job_queue=job_queue,
        job_context=job_context,
        llm=llm,
        rules=rules,
        master_data=master_data,
        projections=projections,
        actual_penalties=actual_penalties,
        projection_service=projection_service,
    )


def get_mitigation_summary_service(
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    summaries: Annotated[PenaltySummaryRepository, Depends(get_penalty_summary_repository)],
    agent_registry: Annotated[AgentRegistryRepository, Depends(get_agent_registry_repository)],
    job_queue: Annotated[JobQueueRepository, Depends(get_job_queue_repository)],
    job_context: Annotated[PenaltyJobItemContextRepository, Depends(get_penalty_job_item_context_repository)],
    llm: Annotated[AzureOpenAIChatClient, Depends(get_llm_client)],
    master_data: Annotated[MasterDataRepository, Depends(get_master_data_repository)],
    mitigation_options: Annotated[MitigationOptionRepository, Depends(get_mitigation_option_repository)],
    actual_penalties: Annotated[ActualPenaltyRepository, Depends(get_actual_penalty_repository)],
    projection_service: Annotated[ProjectionService, Depends(get_projection_service)],
) -> MitigationSummaryService:
    """Provide a mitigation summary service for generating LLM-powered mitigation recommendations."""
    return MitigationSummaryService(
        purchase_orders=purchase_orders,
        summaries=summaries,
        agent_registry=agent_registry,
        job_queue=job_queue,
        job_context=job_context,
        llm=llm,
        master_data=master_data,
        mitigation_options=mitigation_options,
        actual_penalties=actual_penalties,
        projection_service=projection_service,
    )


def get_penalty_rule_extraction_service(
    session: Annotated[Session, Depends(get_session, scope="function")],
    retailer_agreements: Annotated[RetailerAgreementRepository, Depends(get_retailer_agreement_repository)],
    extracted_rules: Annotated[
        ExtractedPenaltyRuleRepository, Depends(get_extracted_penalty_rule_repository)
    ],
    publications: Annotated[RulePublicationRepository, Depends(get_rule_publication_repository)],
    rules: Annotated[PenaltyRuleRepository, Depends(get_penalty_rule_repository)],
    agent_registry: Annotated[AgentRegistryRepository, Depends(get_agent_registry_repository)],
    agent_runs: Annotated[AgentRunRepository, Depends(get_agent_run_repository)],
    master_data: Annotated[MasterDataRepository, Depends(get_master_data_repository)],
    revisions: Annotated[
        ExtractedPenaltyRuleRevisionRepository, Depends(get_extracted_penalty_rule_revision_repository)
    ],
) -> PenaltyRuleExtractionService:
    """Provide a penalty rule extraction service built from per-request repositories.

    Only the compiled extraction graph comes from the process-wide `Container`: it is
    expensive to compile and shares the CMIR/PO-validation checkpointer. Everything else
    is an ordinary per-request repository, like every other penalties provider here.
    """
    return PenaltyRuleExtractionService(
        retailer_agreements=retailer_agreements,
        extracted_rules=extracted_rules,
        publications=publications,
        rules=rules,
        agent_registry=agent_registry,
        agent_runs=agent_runs,
        master_data=master_data,
        revisions=revisions,
        session=session,
        graph=Container.build().rule_extraction_graph,
    )


def get_rule_revision_service(
    session: Annotated[Session, Depends(get_session, scope="function")],
    retailer_agreements: Annotated[RetailerAgreementRepository, Depends(get_retailer_agreement_repository)],
    extracted_rules: Annotated[
        ExtractedPenaltyRuleRepository, Depends(get_extracted_penalty_rule_repository)
    ],
    revisions: Annotated[
        ExtractedPenaltyRuleRevisionRepository, Depends(get_extracted_penalty_rule_revision_repository)
    ],
    agent_runs: Annotated[AgentRunRepository, Depends(get_agent_run_repository)],
) -> RuleRevisionService:
    """Provide a rule revision service, built from per-request repositories like the extraction service above."""
    return RuleRevisionService(
        retailer_agreements=retailer_agreements,
        extracted_rules=extracted_rules,
        revisions=revisions,
        agent_runs=agent_runs,
        session=session,
    )


def get_penalty_seeding_service(
    master_data: Annotated[MasterDataRepository, Depends(get_master_data_repository)],
    retailer_agreements: Annotated[RetailerAgreementRepository, Depends(get_retailer_agreement_repository)],
    rules: Annotated[PenaltyRuleRepository, Depends(get_penalty_rule_repository)],
    purchase_orders: Annotated[PurchaseOrderRepository, Depends(get_purchase_order_repository)],
    fulfillment: Annotated[FulfillmentRepository, Depends(get_fulfillment_repository)],
    projection_service: Annotated[ProjectionService, Depends(get_projection_service)],
    mitigation_inputs: Annotated[MitigationInputRepository, Depends(get_mitigation_input_repository)],
    delivery_change_service: Annotated[
        PoDeliveryChangeRequestService, Depends(get_delivery_change_request_service)
    ],
    delivery_change_requests: Annotated[
        PoDeliveryChangeRequestRepository, Depends(get_delivery_change_request_repository)
    ],
    penalty_summaries: Annotated[PenaltySummaryRepository, Depends(get_penalty_summary_repository)],
    penalty_projections: Annotated[PenaltyProjectionRepository, Depends(get_penalty_projection_repository)],
    actual_penalties: Annotated[ActualPenaltyRepository, Depends(get_actual_penalty_repository)],
    disputes: Annotated[PenaltyDisputeRepository, Depends(get_dispute_repository)],
    job_queue: Annotated[JobQueueRepository, Depends(get_job_queue_repository)],
    penalty_job_item_context: Annotated[
        PenaltyJobItemContextRepository, Depends(get_penalty_job_item_context_repository)
    ],
    penalty_job_run_context: Annotated[
        PenaltyJobRunContextRepository, Depends(get_penalty_job_run_context_repository)
    ],
) -> PenaltySeedingService:
    """Provide a penalty seeding service for populating test data and simulations."""
    return PenaltySeedingService(
        master_data=master_data,
        retailer_agreements=retailer_agreements,
        rules=rules,
        purchase_orders=purchase_orders,
        fulfillment=fulfillment,
        projection_service=projection_service,
        mitigation_inputs=mitigation_inputs,
        delivery_change_service=delivery_change_service,
        delivery_change_requests=delivery_change_requests,
        penalty_summaries=penalty_summaries,
        penalty_projections=penalty_projections,
        actual_penalties=actual_penalties,
        disputes=disputes,
        job_queue=job_queue,
        penalty_job_item_context=penalty_job_item_context,
        penalty_job_run_context=penalty_job_run_context,
    )


# --- CMIR / PO Validation --------------------------------------------------
# Built from app.core.container.Container (a separate composition root, not
# the Depends() chain above) since the CMIR/PO-validation graphs share a
# process-lifetime LangGraph PostgresSaver checkpointer. Session-lifecycle
# fix: `CmirService`/`PoValidationService` no longer hold any repository
# or compiled graph on `self` -- each public method opens its own fresh
# Session per invocation via `Container.cmir_repos`/`cmir_unit_of_work`
# (resp. `po_validation_repos`/`po_validation_unit_of_work`), so what's
# built here is just a reference to those factory methods, not pre-built
# repositories. See app/core/container.py.


def build_service() -> CmirService:
    """Build the production CMIR service from the project composition root."""
    container = Container.build()
    return CmirService(
        email_reader=container.email_reader,
        repos_factory=container.cmir_repos,
        unit_of_work_factory=container.cmir_unit_of_work,
    )


def build_po_validation_service() -> PoValidationService:
    """Build the production PO Validation service from the project composition root."""
    container = Container.build()
    return PoValidationService(
        repos_factory=container.po_validation_repos,
        unit_of_work_factory=container.po_validation_unit_of_work,
    )


def get_service(request: Request) -> CmirService:
    """Return the app-instance-lifetime CMIR service singleton."""
    if request.app.state.service is None:
        request.app.state.service = build_service()
    return request.app.state.service


def get_po_service(request: Request) -> PoValidationService:
    """FastAPI dependency returning the app-instance-lifetime PO Validation service."""
    if request.app.state.po_service is None:
        request.app.state.po_service = build_po_validation_service()
    return request.app.state.po_service


def build_ontology_update_service() -> OntologyUpdateRunService:
    """Build the ontology-update POC's run service from the project
    composition root -- same shape as `build_service`/
    `build_po_validation_service` above, sharing the same `Container`
    (and, through it, the same process-lifetime `PostgresSaver`
    checkpointer) rather than a separate composition root."""
    container = Container.build()
    return OntologyUpdateRunService(unit_of_work_factory=container.ontology_update_unit_of_work)


def get_ontology_update_service(request: Request) -> OntologyUpdateRunService:
    """FastAPI dependency returning the app-instance-lifetime ontology-update
    run service. Memoized on `request.app.state`, exactly as
    `get_service`/`get_po_service` are, so a test-created app carrying a
    fake via `create_app(ontology_update_service=...)` is never silently
    overwritten with the real, Postgres-backed one."""
    if request.app.state.ontology_update_service is None:
        request.app.state.ontology_update_service = build_ontology_update_service()
    return request.app.state.ontology_update_service


def build_ontology_insert_service() -> OntologyInsertRunService:
    """Build the ontology-insert POC's run service from the project
    composition root -- same shape as `build_ontology_update_service`
    above, sharing the same `Container`/`PostgresSaver` checkpointer."""
    container = Container.build()
    return OntologyInsertRunService(unit_of_work_factory=container.ontology_insert_unit_of_work)


def get_ontology_insert_service(request: Request) -> OntologyInsertRunService:
    """FastAPI dependency returning the app-instance-lifetime ontology-insert
    run service. Memoized on `request.app.state`, exactly as
    `get_ontology_update_service` is."""
    if request.app.state.ontology_insert_service is None:
        request.app.state.ontology_insert_service = build_ontology_insert_service()
    return request.app.state.ontology_insert_service


# --- Ontology (CMIR <-> Material Master traceability, component B) --------
# Not memoized on app.state like get_service/get_po_service above:
# OntologyGraphService is a thin, stateless wrapper around whatever graph
# Container currently holds (rebuilt on an interval by
# app.services.ontology.materialize_job -- see app/main.py's lifespan), so
# building a fresh one per request is cheap and, unlike memoizing it, never
# risks handing a caller a graph reference that's gone stale after a rebuild.


def get_ontology_graph_service() -> OntologyGraphService:
    container = Container.build()
    graph = container.get_ontology_graph()
    return OntologyGraphService(graph=graph if graph is not None else RdfGraph())


def get_ontology_context_service() -> OntologyContextService:
    """Schema/relationship context -- unlike `get_ontology_graph_service`,
    this never touches the live rebuilt graph or Postgres at all, only the
    mapping module + a local `.ttl` parse, so it needs no `Container`."""
    return OntologyContextService()
