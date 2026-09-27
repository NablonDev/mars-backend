from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import ClassVar

# Previous implementation using MemorySaver.
# Replaced by PostgreSQL Checkpointer for durable LangGraph resume support.
# from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from rdflib import Graph as RdfGraph

from app.agents.cmir.graph import build_graph
from app.agents.cmir.nodes import WorkflowNodes
from app.agents.ontology_insert.graph import build_ontology_insert_graph
from app.agents.ontology_insert.nodes import OntologyInsertNodes
from app.agents.ontology_update.graph import build_ontology_update_graph
from app.agents.ontology_update.nodes import OntologyUpdateNodes
from app.agents.po_validation.graph import build_po_validation_graph
from app.agents.po_validation.nodes import PoValidationNodes
from app.core.config import EmailConfig, LLMConfig, ServiceBusConfig, Settings, get_settings
from app.db.base import LANGGRAPH_SCHEMA
from app.db.session import Database, checkpoint_dsn
from app.queue.cmir_mail_producer import ServiceBusMailQueue
from app.repositories.cmir.action_log import ActionLogRepository
from app.repositories.cmir.cmir_record import CmirRecordRepository
from app.repositories.cmir.email import EmailRepository
from app.repositories.cmir.job_context import CmirJobItemContextRepository, CmirJobRunContextRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.process.agent_registry import (
    AgentRegistryRepository,
    AgentRunRepository,
    AgentTraceRepository,
)
from app.repositories.process.job_queue import JobQueueRepository
from app.repositories.process.workflow import (
    HumanActionRepository,
    ProcessingErrorRepository,
    WorkflowThreadRepository,
)
from app.services.cli_human_review import CLIHumanReviewPort
from app.services.cmir.extractor import AzureOpenAICmirExtractor
from app.services.cmir.validation import CmirValidator
from app.services.common.material_master_service import MaterialMasterService
from app.services.email_reader import GmailImapReader
from app.services.ontology.context_service import OntologyContextService

logger = logging.getLogger(__name__)


@dataclass
class Container:
    """Composition root for the application and workflow runtime.

    Owns exactly the resources that are genuinely process-lifetime: config,
    the one `Database` (a connection pool + session factory, not a live
    Session), the LangGraph PostgreSQL checkpointer (expensive to set up,
    safe and correct to reuse across every request), and a few stateless
    collaborators (`email_reader`, `validator`, `human_review`,
    `service_bus_queue`).

    It deliberately does NOT hold repositories, LangGraph nodes, or a
    compiled graph as instance state any more -- those are session-bound and
    are built fresh, per invocation, by `cmir_repos`/`cmir_unit_of_work`/
    `po_validation_repos`/`po_validation_unit_of_work` below. (Previously
    this class opened one `Session` here and held every repository/node/
    graph against it for the life of the process -- since nothing ever
    committed that Session, no CMIR/PO-validation mutation was durably
    persisted. See docs/ARCHITECTURE.md and the session-lifecycle
    investigation this fixes.)

    One deliberate exception to that "repos are per-call" rule:
    `_ontology_graph` below. It backs `OntologyGraphService` (the CMIR <->
    Material Master traceability read model) and is process-lifetime state
    on purpose -- it's rebuilt on an interval by
    `app.services.ontology.materialize_job.rebuild`, not per request, so
    holding it here (instead of opening it fresh per call, the way every
    repository above does) is what lets a rebuild's cost be paid once per
    interval rather than once per request. `refresh_ontology_graph`/
    `get_ontology_graph` guard it with `_ontology_graph_lock` so a reader
    never observes a graph mid-rebuild.
    """

    config: Settings
    database: Database
    email_reader: GmailImapReader
    validator: CmirValidator
    human_review: CLIHumanReviewPort
    service_bus_queue: ServiceBusMailQueue
    checkpointer: BaseCheckpointSaver
    _resource_stack: ExitStack
    _ontology_graph: RdfGraph | None = field(default=None, init=False, repr=False)
    _ontology_graph_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    _instance: ClassVar[Container | None] = None

    @classmethod
    def build(cls) -> Container:
        if cls._instance is not None:
            return cls._instance

        config = get_settings()
        resources = ExitStack()

        database = Database(
            config.database.url,
            pool_size=config.database.pool_size,
            max_overflow=config.database.max_overflow,
            pool_timeout=config.database.pool_timeout,
        )

        email_reader = GmailImapReader(EmailConfig.from_settings(config))
        validator = CmirValidator()
        human_review = CLIHumanReviewPort()
        service_bus_queue = ServiceBusMailQueue(ServiceBusConfig.from_settings(config))

        logger.info("Initializing LangGraph PostgreSQL Checkpointer...")
        # Previous implementation using MemorySaver kept for easy rollback.
        # checkpointer = MemorySaver()
        checkpointer = resources.enter_context(
            PostgresSaver.from_conn_string(checkpoint_dsn(config.database.url, LANGGRAPH_SCHEMA))
        )
        checkpointer.setup()
        logger.info("Checkpoint tables verified.")

        cls._instance = cls(
            config=config,
            database=database,
            email_reader=email_reader,
            validator=validator,
            human_review=human_review,
            service_bus_queue=service_bus_queue,
            checkpointer=checkpointer,
            _resource_stack=resources,
        )
        return cls._instance

    @classmethod
    def close(cls) -> None:
        if cls._instance is None:
            return
        cls._instance._resource_stack.close()
        cls._instance.database.dispose()
        cls._instance = None

    # ------------------------------------------------------------------
    # CMIR -- fresh repositories / unit of work, one Session per call
    # ------------------------------------------------------------------

    @contextmanager
    def cmir_repos(self) -> Iterator[SimpleNamespace]:
        """Fresh repositories for `CmirRunService`'s read-only methods --
        one `Session`, committed on success / rolled back on exception /
        closed either way when the caller's `with` block exits (reuses
        `Database.session()`, already correct)."""
        with self.database.session() as session:
            yield SimpleNamespace(
                agent_registry=AgentRegistryRepository(session),
                agent_runs=AgentRunRepository(session),
                agent_traces=AgentTraceRepository(session),
                workflow_threads=WorkflowThreadRepository(session),
                human_actions=HumanActionRepository(session),
                cmir_records=CmirRecordRepository(session),
                email_repository=EmailRepository(session),
                action_log_repository=ActionLogRepository(session),
                job_queue=JobQueueRepository(session),
                job_run_context=CmirJobRunContextRepository(session),
                job_item_context=CmirJobItemContextRepository(session),
            )

    @contextmanager
    def cmir_unit_of_work(self) -> Iterator[SimpleNamespace]:
        """Same Session/repositories as `cmir_repos`, plus a freshly
        compiled graph wired around that Session's repositories -- for
        `CmirRunService`'s graph-touching methods.

        Reuses this Container's one process-lifetime `checkpointer`
        (`PostgresSaver`); only the graph's node closures are rebuilt per
        call, not the checkpointer connection itself, so checkpoint
        behavior is unaffected by how often this is called.
        """
        with self.cmir_repos() as repos:
            extractor = AzureOpenAICmirExtractor(LLMConfig.from_settings(self.config), repos.agent_registry)
            nodes = WorkflowNodes(
                email_reader=self.email_reader,
                extractor=extractor,
                validator=self.validator,
                email_repository=repos.email_repository,
                cmir_repository=repos.cmir_records,
                action_log_repository=repos.action_log_repository,
            )
            graph = build_graph(nodes, self.checkpointer, repos.agent_traces)
            yield SimpleNamespace(**vars(repos), graph=graph)

    # ------------------------------------------------------------------
    # PO Validation -- fresh repositories / unit of work, one Session per call
    # ------------------------------------------------------------------

    @contextmanager
    def po_validation_repos(self) -> Iterator[SimpleNamespace]:
        """Fresh repositories for `PoValidationService`'s read-only methods
        -- same Session lifecycle as `cmir_repos`."""
        with self.database.session() as session:
            yield SimpleNamespace(
                purchase_orders=PurchaseOrderRepository(session),
                master_data=MasterDataRepository(session),
                agent_registry=AgentRegistryRepository(session),
                agent_runs=AgentRunRepository(session),
                agent_traces=AgentTraceRepository(session),
                workflow_threads=WorkflowThreadRepository(session),
                human_actions=HumanActionRepository(session),
                processing_errors=ProcessingErrorRepository(session),
                # PoValidationNodes.validate_against_cmir looks up an existing
                # CMIR mapping -- same repository class as the CMIR domain's,
                # bound to this call's own fresh Session.
                cmir_records=CmirRecordRepository(session),
            )

    @contextmanager
    def po_validation_unit_of_work(self) -> Iterator[SimpleNamespace]:
        """Same Session/repositories as `po_validation_repos`, plus a
        freshly compiled graph -- for `PoValidationService`'s graph-touching
        methods. Shares this Container's one `checkpointer`, exactly as
        `cmir_unit_of_work` does (checkpoint thread_ids are namespaced
        `thread_po_...`, so the two graphs never collide in checkpoint
        storage -- unchanged)."""
        with self.po_validation_repos() as repos:
            nodes = PoValidationNodes(
                purchase_order_repository=repos.purchase_orders,
                master_data_repository=repos.master_data,
                cmir_repository=repos.cmir_records,
                processing_error_repository=repos.processing_errors,
            )
            graph = build_po_validation_graph(nodes, self.checkpointer, repos.agent_traces)
            yield SimpleNamespace(**vars(repos), graph=graph)

    # ------------------------------------------------------------------
    # Ontology (CMIR <-> Material Master traceability) -- fresh
    # repositories per call, but a process-lifetime graph (see class
    # docstring for why the graph is the one exception).
    # ------------------------------------------------------------------

    @contextmanager
    def ontology_repos(self) -> Iterator[SimpleNamespace]:
        """Fresh repositories for `materialize_job.rebuild` -- one Session
        per call, same lifecycle as `cmir_repos`/`po_validation_repos`."""
        with self.database.session() as session:
            yield SimpleNamespace(
                cmir_records=CmirRecordRepository(session),
                master_data=MasterDataRepository(session),
            )

    def refresh_ontology_graph(self, graph: RdfGraph) -> None:
        """Atomically swap in a freshly rebuilt graph. Called by
        `materialize_job.rebuild` after it finishes building `graph` from a
        full bulk read -- never partially, so a concurrent reader always
        sees either the previous complete graph or the new one."""
        with self._ontology_graph_lock:
            self._ontology_graph = graph

    def get_ontology_graph(self) -> RdfGraph | None:
        """The last successfully materialized graph, or `None` if
        `materialize_job` hasn't completed a rebuild yet (e.g. right after
        process startup)."""
        with self._ontology_graph_lock:
            return self._ontology_graph

    # ------------------------------------------------------------------
    # Ontology Update POC (LangGraph HITL agent, Phase 2 Checkpoints 2-4) --
    # fresh repositories/service per call, same shape as
    # cmir_unit_of_work/po_validation_unit_of_work. Deliberately does NOT
    # touch process.workflow_thread/human_action -- self-contained, keyed
    # only on its own checkpoint thread_id (see
    # app.services.ontology_update.run_service's module docstring).
    # ------------------------------------------------------------------

    @contextmanager
    def ontology_update_repos(self) -> Iterator[SimpleNamespace]:
        """Fresh repositories for `OntologyUpdateRunService`'s methods --
        one Session per call, same lifecycle as `cmir_repos`."""
        with self.database.session() as session:
            yield SimpleNamespace(
                master_data=MasterDataRepository(session),
                agent_traces=AgentTraceRepository(session),
            )

    @contextmanager
    def ontology_update_unit_of_work(self) -> Iterator[SimpleNamespace]:
        """Same Session/repositories as `ontology_update_repos`, plus a
        freshly compiled graph wired around that Session's repositories --
        for `OntologyUpdateRunService`'s graph-touching methods. Shares
        this Container's one `checkpointer` (`PostgresSaver`), exactly as
        `cmir_unit_of_work`/`po_validation_unit_of_work` do -- checkpoint
        thread_ids are namespaced `thread_ontology_update_...`
        (`OntologyUpdateRunService._new_thread_id`), so this graph's
        checkpoints never collide with CMIR's (`thread_...`) or PO
        Validation's (`thread_po_...`) in the same checkpoint storage.
        """
        with self.ontology_update_repos() as repos:
            material_master_service = MaterialMasterService(master_data_repository=repos.master_data)
            nodes = OntologyUpdateNodes(
                context_service=OntologyContextService(),
                master_data_repository=repos.master_data,
                material_master_service=material_master_service,
            )
            graph = build_ontology_update_graph(nodes, self.checkpointer, repos.agent_traces)
            yield SimpleNamespace(**vars(repos), graph=graph)

    # ------------------------------------------------------------------
    # Ontology Insert POC (LangGraph HITL agent, sibling to ontology_update
    # above) -- same shape: fresh repositories/service per call, the same
    # shared checkpointer, thread_ids namespaced `thread_ontology_insert_...`
    # so they never collide with any other domain's checkpoints.
    # ------------------------------------------------------------------

    @contextmanager
    def ontology_insert_repos(self) -> Iterator[SimpleNamespace]:
        """Fresh repositories for `OntologyInsertRunService`'s methods --
        one Session per call, same lifecycle as `ontology_update_repos`."""
        with self.database.session() as session:
            yield SimpleNamespace(
                master_data=MasterDataRepository(session),
                agent_traces=AgentTraceRepository(session),
            )

    @contextmanager
    def ontology_insert_unit_of_work(self) -> Iterator[SimpleNamespace]:
        """Same Session/repositories as `ontology_insert_repos`, plus a
        freshly compiled graph -- for `OntologyInsertRunService`'s
        graph-touching methods. Shares this Container's one `checkpointer`
        (`PostgresSaver`), exactly as `ontology_update_unit_of_work` does.
        """
        with self.ontology_insert_repos() as repos:
            material_master_service = MaterialMasterService(master_data_repository=repos.master_data)
            nodes = OntologyInsertNodes(
                context_service=OntologyContextService(),
                master_data_repository=repos.master_data,
                material_master_service=material_master_service,
            )
            graph = build_ontology_insert_graph(nodes, self.checkpointer, repos.agent_traces)
            yield SimpleNamespace(**vars(repos), graph=graph)
