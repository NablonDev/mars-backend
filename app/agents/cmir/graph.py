"""CMIR workflow graph builder.

Orchestrates the email extraction, validation, and merge pipeline. Manages state
across workflow nodes and coordinates with external services for tracing and
checkpoint recovery.
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.agents.cmir.nodes import WorkflowNodes
from app.agents.cmir.state import GraphState
from app.core.tracing import traced
from app.repositories.process.agent_registry import AgentTraceRepository


def build_graph(
    nodes: WorkflowNodes,
    checkpointer: BaseCheckpointSaver,
    trace_repo: AgentTraceRepository,
):
    """Build the CMIR resolution workflow graph.

    Constructs a LangGraph state machine with nodes for email extraction,
    validation, merge, human approval, and persistence. Integrates checkpointing
    for recovery and tracing for observability.
    """
    builder = StateGraph(GraphState)

    def node(name: str, fn):
        """Wrap a WorkflowNodes method with tracing so its invocation is recorded to process.agent_trace."""
        return traced(name, fn, trace_repo)

    builder.add_node("persist_email", node("persist_email", nodes.persist_email))
    builder.add_node("extract_cmir", node("extract_cmir", nodes.extract_cmir))
    builder.add_node("identify_existing_cmir", node("identify_existing_cmir", nodes.identify_existing_cmir))
    builder.add_node("prepare_diff", node("prepare_diff", nodes.prepare_diff))
    builder.add_node("validate_cmir", node("validate_cmir", nodes.validate_cmir))
    builder.add_node("persist_ai_result", node("persist_ai_result", nodes.persist_ai_result))
    builder.add_node("collect_missing_fields", node("collect_missing_fields", nodes.collect_missing_fields))
    builder.add_node("human_approval", node("human_approval", nodes.human_approval))
    builder.add_node("persist_cmir", node("persist_cmir", nodes.persist_cmir))
    builder.add_node("persist_rejection", node("persist_rejection", nodes.persist_rejection))
    builder.add_node(
        "handle_version_conflict", node("handle_version_conflict", nodes.handle_version_conflict)
    )
    builder.add_node("mark_email_read", node("mark_email_read", nodes.mark_email_read))

    builder.add_edge(START, "persist_email")
    builder.add_edge("persist_email", "extract_cmir")
    builder.add_edge("extract_cmir", "identify_existing_cmir")
    builder.add_edge("identify_existing_cmir", "prepare_diff")
    builder.add_edge("prepare_diff", "validate_cmir")
    builder.add_edge("validate_cmir", "persist_ai_result")

    builder.add_conditional_edges(
        "persist_ai_result",
        nodes.route_after_validation,
        {
            "needs_input": "collect_missing_fields",
            "ready": "human_approval",
        },
    )

    # After the human supplies missing values, re-identify the active record before
    # re-validating: the field they just supplied may be the identity field the first
    # lookup was missing (see the module docstring above).
    builder.add_edge("collect_missing_fields", "identify_existing_cmir")

    builder.add_conditional_edges(
        "human_approval",
        nodes.route_after_approval,
        {
            "approved": "persist_cmir",
            "rejected": "persist_rejection",
        },
    )

    builder.add_conditional_edges(
        "persist_cmir",
        nodes.route_after_persist_cmir,
        {
            "committed": "mark_email_read",
            "conflict": "handle_version_conflict",
        },
    )

    builder.add_edge("handle_version_conflict", "mark_email_read")
    builder.add_edge("persist_rejection", "mark_email_read")
    builder.add_edge("mark_email_read", END)

    graph = builder.compile(checkpointer=checkpointer)

    return graph
