"""FastAPI dependency factories for database, repository, service, and queue components."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Generator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import LLMConfig, Settings, get_settings
from app.core.container import Container
from app.core.exceptions import ValidationError
from app.db.session import Database
from app.queue.interfaces import JobDispatcher, JobSource
from app.repositories.cmir.job_context import CmirJobItemContextRepository, CmirJobRunContextRepository
from app.repositories.common.delivery_change_request import PoDeliveryChangeRequestRepository
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.dispute import PenaltyDisputeRepository
from app.repositories.penalties.job_context import (
    PenaltyJobItemContextRepository,
    PenaltyJobRunContextRepository,
)
from app.repositories.penalties.mitigation import MitigationInputRepository, MitigationOptionRepository
from app.repositories.penalties.projection import ActualPenaltyRepository, PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.rule_extraction import (
    ExtractedPenaltyRuleRepository,
    RulePublicationRepository,
)
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.repositories.process.workflow import HumanActionRepository, WorkflowThreadRepository
from app.services.cmir.service import CmirService
from app.services.penalties.delivery_change import PoDeliveryChangeRequestService
from app.services.penalties.dispute.service import DisputeResolutionService
from app.services.penalties.dispute.summary_service import DisputeSummaryService
from app.services.penalties.mitigation.service import MitigationService
from app.services.penalties.mitigation.summary_service import MitigationSummaryService
from app.services.penalties.projection.service import ProjectionService
from app.services.penalties.projection.summary_service import ProjectionSummaryService
from app.services.penalties.rule_extraction.service import PenaltyRuleExtractionService
from app.services.po_validation.service import PoValidationService
from app.services.seeding.service import PenaltySeedingService

logger = logging.getLogger(__name__)


def require_internal_api_key(
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-Api-Key"),
    settings: Settings = Depends(get_settings),
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

    def _dependency(include: str | None = Query(default=None)) -> set[str]:
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


def get_session(database: Database = Depends(get_database)) -> Generator[Session, None, None]:
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
    session: Session = Depends(get_session, scope="function"),
) -> MasterDataRepository:
    """Provide a master data repository for reading carrier, plant, and SKU data."""
    return MasterDataRepository(session)


def get_purchase_order_repository(
    session: Session = Depends(get_session, scope="function"),
) -> PurchaseOrderRepository:
    """Provide a purchase order repository for reading and writing PO headers and lines."""
    return PurchaseOrderRepository(session)


def get_fulfillment_repository(
    session: Session = Depends(get_session, scope="function"),
) -> FulfillmentRepository:
    """Provide a fulfillment repository for reading and writing shipments and demand exceptions."""
    return FulfillmentRepository(session)


# ---------------------------------------------------------------------------
# process: repositories (shared backbone)
# ---------------------------------------------------------------------------


def get_job_queue_repository(session: Session = Depends(get_session, scope="function")) -> JobQueueRepository:
    """Provide a job queue repository for managing background job execution state."""
    return JobQueueRepository(session)


def get_agent_registry_repository(
    session: Session = Depends(get_session, scope="function"),
) -> AgentRegistryRepository:
    """Provide an agent registry repository for tracking agent state and runs."""
    return AgentRegistryRepository(session)


def get_agent_run_repository(session: Session = Depends(get_session, scope="function")) -> AgentRunRepository:
    """Provide an agent-run repository bound to the request session."""
    return AgentRunRepository(session)


# ---------------------------------------------------------------------------
# penalties: repositories
# ---------------------------------------------------------------------------


def get_penalty_rule_repository(
    session: Session = Depends(get_session, scope="function"),
) -> PenaltyRuleRepository:
    """Provide a penalty rule repository for accessing rule configurations."""
    return PenaltyRuleRepository(session)


def get_penalty_projection_repository(
    session: Session = Depends(get_session, scope="function"),
) -> PenaltyProjectionRepository:
    """Provide a penalty projection repository for reading and writing projections."""
    return PenaltyProjectionRepository(session)


def get_actual_penalty_repository(
    session: Session = Depends(get_session, scope="function"),
) -> ActualPenaltyRepository:
    """Provide an actual penalty repository for reading and writing realized penalties."""
    return ActualPenaltyRepository(session)


def get_penalty_summary_repository(
    session: Session = Depends(get_session, scope="function"),
) -> PenaltySummaryRepository:
    """Provide a penalty summary repository for accessing summary job data."""
    return PenaltySummaryRepository(session)


def get_mitigation_input_repository(
    session: Session = Depends(get_session, scope="function"),
) -> MitigationInputRepository:
    """Provide a mitigation input repository for reading mitigation configuration."""
    return MitigationInputRepository(session)


def get_mitigation_option_repository(
    session: Session = Depends(get_session, scope="function"),
) -> MitigationOptionRepository:
    """Provide a mitigation option repository for reading and writing computed options."""
    return MitigationOptionRepository(session)


def get_dispute_repository(
    session: Session = Depends(get_session, scope="function"),
) -> PenaltyDisputeRepository:
    """Provide a dispute repository for reading and writing penalty disputes."""
    return PenaltyDisputeRepository(session)


def get_delivery_change_request_repository(
    session: Session = Depends(get_session, scope="function"),
) -> PoDeliveryChangeRequestRepository:
    """Provide a delivery change request repository for tracking PO delivery modifications."""
    return PoDeliveryChangeRequestRepository(session)


def get_penalty_job_item_context_repository(
    session: Session = Depends(get_session, scope="function"),
) -> PenaltyJobItemContextRepository:
    """Provide a penalty job item context repository for job execution details."""
    return PenaltyJobItemContextRepository(session)


def get_penalty_job_run_context_repository(
    session: Session = Depends(get_session, scope="function"),
) -> PenaltyJobRunContextRepository:
    """Provide a penalty job run context repository for top-level job state."""
    return PenaltyJobRunContextRepository(session)


def get_retailer_agreement_repository(
    session: Session = Depends(get_session, scope="function"),
) -> RetailerAgreementRepository:
    """Provide a retailer agreement repository for reading and writing retailer agreement documents."""
    return RetailerAgreementRepository(session)


def get_extracted_penalty_rule_repository(
    session: Session = Depends(get_session, scope="function"),
) -> ExtractedPenaltyRuleRepository:
    """Provide an extracted penalty rule repository for the review-and-publication staging area."""
    return ExtractedPenaltyRuleRepository(session)


def get_rule_publication_repository(
    session: Session = Depends(get_session, scope="function"),
) -> RulePublicationRepository:
    """Provide a rule publication repository for the append-only publication audit trail."""
    return RulePublicationRepository(session)


def get_workflow_thread_repository(
    session: Session = Depends(get_session, scope="function"),
) -> WorkflowThreadRepository:
    """Provide a workflow thread repository bound to the request session."""
    return WorkflowThreadRepository(session)


def get_human_action_repository(
    session: Session = Depends(get_session, scope="function"),
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
    job_queue: tuple[JobDispatcher, JobSource] = Depends(get_job_queue),
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


def get_llm_client(request: Request, settings: Settings = Depends(get_settings)) -> AzureOpenAIChatClient:
    """Provide the application-scoped Azure OpenAI LLM client."""
    return get_llm_client_for_app(request.app, settings)


# ---------------------------------------------------------------------------
# penalties: services
# ---------------------------------------------------------------------------


def get_projection_service(
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    fulfillment: FulfillmentRepository = Depends(get_fulfillment_repository),
    rules: PenaltyRuleRepository = Depends(get_penalty_rule_repository),
    master_data: MasterDataRepository = Depends(get_master_data_repository),
    projections: PenaltyProjectionRepository = Depends(get_penalty_projection_repository),
) -> ProjectionService:
    """Provide a penalty projection service for computing penalty exposure."""
    return ProjectionService(
        purchase_orders=purchase_orders,
        fulfillment=fulfillment,
        rules=rules,
        master_data=master_data,
        projections=projections,
    )


def get_mitigation_service(
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    rules: PenaltyRuleRepository = Depends(get_penalty_rule_repository),
    master_data: MasterDataRepository = Depends(get_master_data_repository),
    projections: PenaltyProjectionRepository = Depends(get_penalty_projection_repository),
    mitigation_inputs: MitigationInputRepository = Depends(get_mitigation_input_repository),
    mitigation_options: MitigationOptionRepository = Depends(get_mitigation_option_repository),
    projection_service: ProjectionService = Depends(get_projection_service),
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
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    delivery_change_requests: PoDeliveryChangeRequestRepository = Depends(
        get_delivery_change_request_repository
    ),
    projection_service: ProjectionService = Depends(get_projection_service),
    master_data: MasterDataRepository = Depends(get_master_data_repository),
) -> PoDeliveryChangeRequestService:
    """Provide a delivery change request service for managing delivery date modifications."""
    return PoDeliveryChangeRequestService(
        purchase_orders=purchase_orders,
        delivery_change_requests=delivery_change_requests,
        projection_service=projection_service,
        master_data=master_data,
    )


def get_dispute_service(
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    disputes: PenaltyDisputeRepository = Depends(get_dispute_repository),
    actual_penalties: ActualPenaltyRepository = Depends(get_actual_penalty_repository),
    rules: PenaltyRuleRepository = Depends(get_penalty_rule_repository),
    projection_service: ProjectionService = Depends(get_projection_service),
    retailer_agreements: RetailerAgreementRepository = Depends(get_retailer_agreement_repository),
    settings: Settings = Depends(get_settings),
) -> DisputeResolutionService:
    """Provide a dispute service for opening and managing penalty disputes."""
    return DisputeResolutionService(
        purchase_orders=purchase_orders,
        disputes=disputes,
        actual_penalties=actual_penalties,
        rules=rules,
        projection_service=projection_service,
        retailer_agreements=retailer_agreements,
        default_window_days=settings.dispute.default_window_days,
    )


def get_dispute_summary_service(
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    summaries: PenaltySummaryRepository = Depends(get_penalty_summary_repository),
    agent_registry: AgentRegistryRepository = Depends(get_agent_registry_repository),
    job_queue: JobQueueRepository = Depends(get_job_queue_repository),
    job_context: PenaltyJobItemContextRepository = Depends(get_penalty_job_item_context_repository),
    llm: AzureOpenAIChatClient = Depends(get_llm_client),
    disputes: PenaltyDisputeRepository = Depends(get_dispute_repository),
    rules: PenaltyRuleRepository = Depends(get_penalty_rule_repository),
    master_data: MasterDataRepository = Depends(get_master_data_repository),
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
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    summaries: PenaltySummaryRepository = Depends(get_penalty_summary_repository),
    agent_registry: AgentRegistryRepository = Depends(get_agent_registry_repository),
    job_queue: JobQueueRepository = Depends(get_job_queue_repository),
    job_context: PenaltyJobItemContextRepository = Depends(get_penalty_job_item_context_repository),
    llm: AzureOpenAIChatClient = Depends(get_llm_client),
    rules: PenaltyRuleRepository = Depends(get_penalty_rule_repository),
    master_data: MasterDataRepository = Depends(get_master_data_repository),
    projections: PenaltyProjectionRepository = Depends(get_penalty_projection_repository),
    actual_penalties: ActualPenaltyRepository = Depends(get_actual_penalty_repository),
    projection_service: ProjectionService = Depends(get_projection_service),
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
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    summaries: PenaltySummaryRepository = Depends(get_penalty_summary_repository),
    agent_registry: AgentRegistryRepository = Depends(get_agent_registry_repository),
    job_queue: JobQueueRepository = Depends(get_job_queue_repository),
    job_context: PenaltyJobItemContextRepository = Depends(get_penalty_job_item_context_repository),
    llm: AzureOpenAIChatClient = Depends(get_llm_client),
    master_data: MasterDataRepository = Depends(get_master_data_repository),
    mitigation_options: MitigationOptionRepository = Depends(get_mitigation_option_repository),
    actual_penalties: ActualPenaltyRepository = Depends(get_actual_penalty_repository),
    projection_service: ProjectionService = Depends(get_projection_service),
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
    session: Session = Depends(get_session, scope="function"),
    retailer_agreements: RetailerAgreementRepository = Depends(get_retailer_agreement_repository),
    extracted_rules: ExtractedPenaltyRuleRepository = Depends(get_extracted_penalty_rule_repository),
    publications: RulePublicationRepository = Depends(get_rule_publication_repository),
    rules: PenaltyRuleRepository = Depends(get_penalty_rule_repository),
    agent_registry: AgentRegistryRepository = Depends(get_agent_registry_repository),
    agent_runs: AgentRunRepository = Depends(get_agent_run_repository),
    master_data: MasterDataRepository = Depends(get_master_data_repository),
    workflow_threads: WorkflowThreadRepository = Depends(get_workflow_thread_repository),
    human_actions: HumanActionRepository = Depends(get_human_action_repository),
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
        workflow_threads=workflow_threads,
        human_actions=human_actions,
        session=session,
        graph=Container.build().rule_extraction_graph,
    )


def get_penalty_seeding_service(
    master_data: MasterDataRepository = Depends(get_master_data_repository),
    retailer_agreements: RetailerAgreementRepository = Depends(get_retailer_agreement_repository),
    rules: PenaltyRuleRepository = Depends(get_penalty_rule_repository),
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    fulfillment: FulfillmentRepository = Depends(get_fulfillment_repository),
    projection_service: ProjectionService = Depends(get_projection_service),
    mitigation_inputs: MitigationInputRepository = Depends(get_mitigation_input_repository),
    delivery_change_service: PoDeliveryChangeRequestService = Depends(get_delivery_change_request_service),
    delivery_change_requests: PoDeliveryChangeRequestRepository = Depends(
        get_delivery_change_request_repository
    ),
    penalty_summaries: PenaltySummaryRepository = Depends(get_penalty_summary_repository),
    penalty_projections: PenaltyProjectionRepository = Depends(get_penalty_projection_repository),
    actual_penalties: ActualPenaltyRepository = Depends(get_actual_penalty_repository),
    disputes: PenaltyDisputeRepository = Depends(get_dispute_repository),
    job_queue: JobQueueRepository = Depends(get_job_queue_repository),
    penalty_job_item_context: PenaltyJobItemContextRepository = Depends(
        get_penalty_job_item_context_repository
    ),
    penalty_job_run_context: PenaltyJobRunContextRepository = Depends(get_penalty_job_run_context_repository),
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
# the Depends() chain above) since the CMIR/PO-validation graphs wire up a
# shared LangGraph PostgresSaver checkpointer and long-lived resources that
# don't fit a per-request Session lifecycle. See app/core/container.py.


def _build_cmir_job_context_repositories(
    container: Container,
) -> tuple[JobQueueRepository, CmirJobRunContextRepository, CmirJobItemContextRepository]:
    """Build job context repositories from a dedicated session on the container database."""
    database = Database(
        container.config.database.url,
        pool_size=container.config.database.pool_size,
        max_overflow=container.config.database.max_overflow,
        pool_timeout=container.config.database.pool_timeout,
    )
    session = database.new_session()
    return (
        JobQueueRepository(session),
        CmirJobRunContextRepository(session),
        CmirJobItemContextRepository(session),
    )


def build_service() -> CmirService:
    """Build the production CMIR service from the project composition root."""
    container = Container.build()
    job_queue, job_run_context, job_item_context = _build_cmir_job_context_repositories(container)
    return CmirService(
        email_reader=container.email_reader,
        graph=container.graph,
        agent_registry=container.agent_registry,
        agent_runs=container.agent_runs,
        workflow_threads=container.workflow_threads,
        human_actions=container.human_actions,
        cmir_records=container.cmir_repository,
        job_queue=job_queue,
        job_run_context=job_run_context,
        job_item_context=job_item_context,
        email_repository=container.email_repository,
    )


def build_po_validation_service() -> PoValidationService:
    """Build the production PO Validation service from the project composition root."""
    container = Container.build()
    job_queue, job_run_context, job_item_context = _build_cmir_job_context_repositories(container)
    return PoValidationService(
        graph=container.po_validation_graph,
        purchase_orders=container.purchase_orders,
        master_data=container.master_data,
        agent_registry=container.agent_registry,
        agent_runs=container.agent_runs,
        workflow_threads=container.workflow_threads,
        human_actions=container.human_actions,
        processing_errors=container.processing_errors,
        job_queue=job_queue,
        job_run_context=job_run_context,
        job_item_context=job_item_context,
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
