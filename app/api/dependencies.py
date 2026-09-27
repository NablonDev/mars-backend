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
  `CmirRunService`/`PoValidationService` a reference to `Container`'s
  `cmir_repos`/`cmir_unit_of_work` (resp. `po_validation_repos`/
  `po_validation_unit_of_work`) factory methods -- neither service holds a
  repository or compiled graph on `self` any more (session-lifecycle fix:
  each public method opens its own fresh Session per invocation via one of
  those factories, committed on success/rolled back on exception/closed
  either way). `Container` now also builds the `process.job_queue`/
  `cmir.cmir_job_*_context` repositories `CmirRunService` needs, as part of
  `cmir_repos`, instead of this module opening a second, separate session
  for them.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from rdflib import Graph as RdfGraph
from sqlalchemy.orm import Session

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import LLMConfig, Settings, get_settings
from app.core.container import Container
from app.core.exceptions import ValidationError
from app.db.session import Database
from app.queue.interfaces import JobDispatcher, JobSource
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.delivery_change_request import PoDeliveryChangeRequestRepository
from app.repositories.penalties.job_context import (
    PenaltyJobItemContextRepository,
    PenaltyJobRunContextRepository,
)
from app.repositories.penalties.mitigation import MitigationInputRepository, MitigationOptionRepository
from app.repositories.penalties.projection import ActualPenaltyRepository, PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.cmir.run_service import CmirRunService
from app.services.ontology.context_service import OntologyContextService
from app.services.ontology.graph_service import OntologyGraphService
from app.services.ontology_insert.run_service import OntologyInsertRunService
from app.services.ontology_update.run_service import OntologyUpdateRunService
from app.services.penalties.delivery_change import PoDeliveryChangeRequestService
from app.services.penalties.mitigation.service import MitigationService
from app.services.penalties.mitigation.summary_service import MitigationSummaryService
from app.services.penalties.projection.service import ProjectionService
from app.services.penalties.projection.summary_service import ProjectionSummaryService
from app.services.po_validation.service import PoValidationService
from app.services.seeding.service import PenaltySeedingService

logger = logging.getLogger(__name__)


def require_internal_api_key(
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-Api-Key"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Gate every non-health route behind a shared-secret header."""
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
    """Build a `?include=` query-param dependency validated against a
    route-specific allow-list (approved plan §5) -- pure read, never
    schedules generation as a side effect; every call site here composes
    with an already-cached `get_status()`/repository read, never
    `get_or_schedule()`.
    """

    def _dependency(include: str | None = Query(default=None)) -> set[str]:
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


def get_session(database: Database = Depends(get_database)) -> Iterator[Session]:
    session = database.new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# common -- repositories
# ---------------------------------------------------------------------------


def get_master_data_repository(session: Session = Depends(get_session)) -> MasterDataRepository:
    return MasterDataRepository(session)


def get_purchase_order_repository(session: Session = Depends(get_session)) -> PurchaseOrderRepository:
    return PurchaseOrderRepository(session)


def get_fulfillment_repository(session: Session = Depends(get_session)) -> FulfillmentRepository:
    return FulfillmentRepository(session)


# ---------------------------------------------------------------------------
# process -- repositories (shared backbone)
# ---------------------------------------------------------------------------


def get_job_queue_repository(session: Session = Depends(get_session)) -> JobQueueRepository:
    return JobQueueRepository(session)


def get_agent_registry_repository(session: Session = Depends(get_session)) -> AgentRegistryRepository:
    return AgentRegistryRepository(session)


# ---------------------------------------------------------------------------
# penalties -- repositories
# ---------------------------------------------------------------------------


def get_penalty_rule_repository(session: Session = Depends(get_session)) -> PenaltyRuleRepository:
    return PenaltyRuleRepository(session)


def get_penalty_projection_repository(
    session: Session = Depends(get_session),
) -> PenaltyProjectionRepository:
    return PenaltyProjectionRepository(session)


def get_actual_penalty_repository(session: Session = Depends(get_session)) -> ActualPenaltyRepository:
    return ActualPenaltyRepository(session)


def get_penalty_summary_repository(session: Session = Depends(get_session)) -> PenaltySummaryRepository:
    return PenaltySummaryRepository(session)


def get_mitigation_input_repository(session: Session = Depends(get_session)) -> MitigationInputRepository:
    return MitigationInputRepository(session)


def get_mitigation_option_repository(session: Session = Depends(get_session)) -> MitigationOptionRepository:
    return MitigationOptionRepository(session)


def get_delivery_change_request_repository(
    session: Session = Depends(get_session),
) -> PoDeliveryChangeRequestRepository:
    return PoDeliveryChangeRequestRepository(session)


def get_penalty_job_item_context_repository(
    session: Session = Depends(get_session),
) -> PenaltyJobItemContextRepository:
    return PenaltyJobItemContextRepository(session)


def get_penalty_job_run_context_repository(
    session: Session = Depends(get_session),
) -> PenaltyJobRunContextRepository:
    return PenaltyJobRunContextRepository(session)


# ---------------------------------------------------------------------------
# job queue dispatch -- POST /job-runs only (summary generation enqueues and
# commits its own job_run/job_item internally, see ProjectionSummaryService/
# MitigationSummaryService.get_or_schedule -- no separate dispatch wiring
# needed for those routes this phase; a real consumer worker for either is
# out of scope, see the phase report).
# ---------------------------------------------------------------------------


def get_job_queue(request: Request) -> tuple[JobDispatcher, JobSource]:
    """Return the application-scoped job dispatcher and source."""
    return request.app.state.job_queue


def get_job_dispatcher(
    job_queue: tuple[JobDispatcher, JobSource] = Depends(get_job_queue),
) -> JobDispatcher:
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
    return get_llm_client_for_app(request.app, settings)


# ---------------------------------------------------------------------------
# penalties -- services
# ---------------------------------------------------------------------------


def get_projection_service(
    purchase_orders: PurchaseOrderRepository = Depends(get_purchase_order_repository),
    fulfillment: FulfillmentRepository = Depends(get_fulfillment_repository),
    rules: PenaltyRuleRepository = Depends(get_penalty_rule_repository),
    master_data: MasterDataRepository = Depends(get_master_data_repository),
    projections: PenaltyProjectionRepository = Depends(get_penalty_projection_repository),
) -> ProjectionService:
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
    return PoDeliveryChangeRequestService(
        purchase_orders=purchase_orders,
        delivery_change_requests=delivery_change_requests,
        projection_service=projection_service,
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


def get_penalty_seeding_service(
    master_data: MasterDataRepository = Depends(get_master_data_repository),
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
    job_queue: JobQueueRepository = Depends(get_job_queue_repository),
    penalty_job_item_context: PenaltyJobItemContextRepository = Depends(
        get_penalty_job_item_context_repository
    ),
    penalty_job_run_context: PenaltyJobRunContextRepository = Depends(get_penalty_job_run_context_repository),
) -> PenaltySeedingService:
    return PenaltySeedingService(
        master_data=master_data,
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
        job_queue=job_queue,
        penalty_job_item_context=penalty_job_item_context,
        penalty_job_run_context=penalty_job_run_context,
    )


# --- CMIR / PO Validation --------------------------------------------------
# Built from app.core.container.Container (a separate composition root, not
# the Depends() chain above) since the CMIR/PO-validation graphs share a
# process-lifetime LangGraph PostgresSaver checkpointer. Session-lifecycle
# fix: `CmirRunService`/`PoValidationService` no longer hold any repository
# or compiled graph on `self` -- each public method opens its own fresh
# Session per invocation via `Container.cmir_repos`/`cmir_unit_of_work`
# (resp. `po_validation_repos`/`po_validation_unit_of_work`), so what's
# built here is just a reference to those factory methods, not pre-built
# repositories. See app/core/container.py.


def build_service() -> CmirRunService:
    """Build the production CMIR service from the project composition root."""
    container = Container.build()
    return CmirRunService(
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


def get_service(request: Request) -> CmirRunService:
    """FastAPI dependency returning the app-instance-lifetime CMIR service.

    Memoized on `request.app.state` (not a plain lru_cache) so each FastAPI
    app instance -- including a test-created one that already carries a fake
    via create_app(service=...) -- gets its own singleton instead of sharing
    one across the process.
    """
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
