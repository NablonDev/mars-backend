"""Composition root wiring config, repositories, agents, and LangGraph runtimes into a singleton Container."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from typing import ClassVar

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph.state import CompiledStateGraph

from app.agents.cmir.graph import build_graph
from app.agents.cmir.nodes import WorkflowNodes
from app.agents.penalties.rule_extraction import adapter as rule_extraction_adapter
from app.agents.penalties.rule_extraction.context import ClauseClassificationContext, RuleFactContext
from app.agents.penalties.rule_extraction.graph import build_graph as build_rule_extraction_graph
from app.agents.penalties.rule_extraction.nodes import RuleExtractionNodes
from app.agents.penalties.rule_extraction.schema import PenaltyFactList, PenaltyRuleExtraction
from app.agents.po_validation.graph import build_po_validation_graph
from app.agents.po_validation.nodes import PoValidationNodes
from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import EmailConfig, LLMConfig, ServiceBusConfig, Settings, get_settings
from app.db.base import LANGGRAPH_SCHEMA
from app.db.session import Database, checkpoint_dsn
from app.queue.cmir_mail_producer import ServiceBusMailQueue
from app.repositories.cmir.action_log import ActionLogRepository
from app.repositories.cmir.cmir_record import CmirRecordRepository
from app.repositories.cmir.email import EmailRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.process.agent_registry import (
    AgentRegistryRepository,
    AgentRunRepository,
    AgentTraceRepository,
)
from app.repositories.process.workflow import (
    HumanActionRepository,
    ProcessingErrorRepository,
    WorkflowThreadRepository,
)
from app.services.cli_human_review import CLIHumanReviewPort
from app.services.cmir.extractor import AzureOpenAICmirExtractor
from app.services.cmir.validation import CmirValidator
from app.services.email_reader import GmailImapReader

logger = logging.getLogger(__name__)


@dataclass
class Container:
    """Composition root for the application and workflow runtime."""

    config: Settings
    email_reader: GmailImapReader
    human_review: CLIHumanReviewPort
    graph: CompiledStateGraph
    agent_registry: AgentRegistryRepository
    agent_runs: AgentRunRepository
    agent_traces: AgentTraceRepository
    workflow_threads: WorkflowThreadRepository
    human_actions: HumanActionRepository
    email_repository: EmailRepository
    cmir_repository: CmirRecordRepository
    action_log_repository: ActionLogRepository
    service_bus_queue: ServiceBusMailQueue
    po_validation_graph: CompiledStateGraph
    rule_extraction_graph: CompiledStateGraph
    rule_extraction_classify: Callable[[ClauseClassificationContext], PenaltyRuleExtraction]
    rule_extraction_extract_facts: Callable[[RuleFactContext], PenaltyFactList]
    purchase_orders: PurchaseOrderRepository
    master_data: MasterDataRepository
    processing_errors: ProcessingErrorRepository
    _resource_stack: ExitStack

    _instance: ClassVar[Container | None] = None

    @classmethod
    def build(cls) -> Container:
        """Construct (or return the cached) process-wide Container.

        Wires repositories, agent nodes, and both LangGraph graphs (CMIR
        and PO validation) against one shared database session and one
        shared PostgresSaver checkpointer, then caches the result on
        `_instance` for subsequent calls.
        """
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
        # The container is process-scoped rather than request-scoped, so its
        # repositories share one session for the lifetime of the container.
        # This is intentionally different from the usual per-request session
        # lifecycle used by application endpoints.
        session = database.new_session()
        resources.callback(session.close)

        email_reader = GmailImapReader(EmailConfig.from_settings(config))
        validator = CmirValidator()

        email_repository = EmailRepository(session)
        cmir_repository = CmirRecordRepository(session)
        action_log_repository = ActionLogRepository(session)

        agent_registry_repository = AgentRegistryRepository(session)
        agent_run_repository = AgentRunRepository(session)
        agent_trace_repository = AgentTraceRepository(session)
        workflow_thread_repository = WorkflowThreadRepository(session)
        human_action_repository = HumanActionRepository(session)

        extractor = AzureOpenAICmirExtractor(LLMConfig.from_settings(config), agent_registry_repository)

        human_review = CLIHumanReviewPort()
        service_bus_queue = ServiceBusMailQueue(ServiceBusConfig.from_settings(config))

        nodes = WorkflowNodes(
            email_reader=email_reader,
            extractor=extractor,
            validator=validator,
            email_repository=email_repository,
            cmir_repository=cmir_repository,
            action_log_repository=action_log_repository,
        )

        logger.info("Initializing LangGraph PostgreSQL Checkpointer...")
        checkpointer = resources.enter_context(
            PostgresSaver.from_conn_string(checkpoint_dsn(config.database.url, LANGGRAPH_SCHEMA))
        )
        checkpointer.setup()
        logger.info("Checkpoint tables verified.")
        graph = build_graph(nodes, checkpointer, agent_trace_repository)
        logger.info("Graph compiled with PostgreSQL Checkpointer.")

        purchase_order_repository = PurchaseOrderRepository(session)
        master_data_repository = MasterDataRepository(session)
        processing_error_repository = ProcessingErrorRepository(session)

        po_validation_nodes = PoValidationNodes(
            purchase_order_repository=purchase_order_repository,
            master_data_repository=master_data_repository,
            cmir_repository=cmir_repository,
            processing_error_repository=processing_error_repository,
        )
        # All graphs share the same checkpointer. Each workflow uses a distinct
        # thread-id namespace so checkpoints from different workflows cannot
        # overlap in the shared checkpoint store.
        po_validation_graph = build_po_validation_graph(
            po_validation_nodes, checkpointer, agent_trace_repository
        )
        logger.info("PO Validation graph compiled with PostgreSQL Checkpointer.")

        # Rule extraction is compiled once with the container because graph
        # compilation and the shared checkpointer are intentionally process-scoped.
        # The graph's nodes create their own scoped database sessions at execution
        # time instead of retaining the container's long-lived repository session.
        rule_extraction_client = AzureOpenAIChatClient(LLMConfig.from_settings(config))

        # Keep these bound callables on the container because rule-revision jobs
        # invoke the same adapters directly, outside the graph. Keeping the
        # callables here ensures those jobs use the same client/database bindings
        # as the graph nodes.
        rule_extraction_classify = partial(rule_extraction_adapter.classify, rule_extraction_client, database)
        rule_extraction_extract_facts = partial(
            rule_extraction_adapter.extract_facts, rule_extraction_client, database
        )
        rule_extraction_graph = build_rule_extraction_graph(
            RuleExtractionNodes(
                screen=partial(rule_extraction_adapter.screen, rule_extraction_client, database),
                classify=rule_extraction_classify,
                extract_facts=rule_extraction_extract_facts,
                database=database,
            ),
            checkpointer,
            agent_trace_repository,
        )
        logger.info("Penalty rule extraction graph compiled with PostgreSQL Checkpointer.")

        cls._instance = cls(
            config=config,
            email_reader=email_reader,
            human_review=human_review,
            graph=graph,
            agent_registry=agent_registry_repository,
            agent_runs=agent_run_repository,
            agent_traces=agent_trace_repository,
            workflow_threads=workflow_thread_repository,
            human_actions=human_action_repository,
            email_repository=email_repository,
            cmir_repository=cmir_repository,
            action_log_repository=action_log_repository,
            service_bus_queue=service_bus_queue,
            po_validation_graph=po_validation_graph,
            rule_extraction_graph=rule_extraction_graph,
            rule_extraction_classify=rule_extraction_classify,
            rule_extraction_extract_facts=rule_extraction_extract_facts,
            purchase_orders=purchase_order_repository,
            master_data=master_data_repository,
            processing_errors=processing_error_repository,
            _resource_stack=resources,
        )
        return cls._instance

    @classmethod
    def close(cls) -> None:
        """Tear down the cached Container's resources and clear the singleton so the next build() starts fresh."""
        if cls._instance is None:
            return
        cls._instance._resource_stack.close()
        cls._instance = None
