"""PO validation workflow graph builder.

Orchestrates the CMIR lookup, material-master check, and human-decision pipeline
for a single purchase-order line. Manages state across workflow nodes and
coordinates with external services for tracing and checkpoint recovery.
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from app.agents.po_validation.nodes import PoValidationNodes
from app.agents.po_validation.state import POGraphState
from app.core.tracing import traced
from app.repositories.process.agent_registry import AgentTraceRepository


def build_po_validation_graph(
    nodes: PoValidationNodes,
    checkpointer: BaseCheckpointSaver,
    trace_repo: AgentTraceRepository,
):
    """Build the PO validation workflow graph with checkpointing and tracing."""
    builder = StateGraph(POGraphState)

    def node(name: str, fn):
        """Wrap a WorkflowNodes method with tracing so its invocation is recorded to process.agent_trace."""
        return traced(name, fn, trace_repo)

    builder.add_node("persist_po_line", node("persist_po_line", nodes.persist_po_line))
    builder.add_node("validate_against_cmir", node("validate_against_cmir", nodes.validate_against_cmir))
    builder.add_node("check_material_master", node("check_material_master", nodes.check_material_master))
    builder.add_node(
        "human_manual_cmir_entry", node("human_manual_cmir_entry", nodes.human_manual_cmir_entry)
    )
    builder.add_node("create_cmir_record", node("create_cmir_record", nodes.create_cmir_record))
    builder.add_node(
        "human_qty_mismatch_decision", node("human_qty_mismatch_decision", nodes.human_qty_mismatch_decision)
    )
    builder.add_node(
        "mark_ready_for_so_creation", node("mark_ready_for_so_creation", nodes.mark_ready_for_so_creation)
    )
    builder.add_node(
        "mark_ready_for_so_creation_partial",
        node("mark_ready_for_so_creation_partial", nodes.mark_ready_for_so_creation_partial),
    )
    builder.add_node("mark_discontinued", node("mark_discontinued", nodes.mark_discontinued))
    builder.add_node("handle_error", node("handle_error", nodes.handle_error))

    builder.set_entry_point("persist_po_line")

    builder.add_conditional_edges(
        "persist_po_line",
        nodes.route_after_persist,
        {"error": "handle_error", "continue": "validate_against_cmir"},
    )

    builder.add_conditional_edges(
        "validate_against_cmir",
        nodes.route_after_cmir_validation,
        {
            "error": "handle_error",
            "found": "check_material_master",
            "not_found": "human_manual_cmir_entry",
        },
    )

    builder.add_edge("human_manual_cmir_entry", "create_cmir_record")

    builder.add_conditional_edges(
        "create_cmir_record",
        nodes.route_after_create_cmir_record,
        {"error": "handle_error", "continue": "check_material_master"},
    )

    builder.add_conditional_edges(
        "check_material_master",
        nodes.route_after_material_check,
        {
            "error": "handle_error",
            "sufficient": "mark_ready_for_so_creation",
            "insufficient": "human_qty_mismatch_decision",
        },
    )

    builder.add_conditional_edges(
        "human_qty_mismatch_decision",
        nodes.route_after_qty_mismatch,
        {
            "use_substitute": "check_material_master",
            "proceed_anyway": "mark_ready_for_so_creation_partial",
            "mark_stale": "mark_discontinued",
        },
    )

    builder.add_edge("mark_ready_for_so_creation", END)
    builder.add_edge("mark_ready_for_so_creation_partial", END)
    builder.add_edge("mark_discontinued", END)
    builder.add_edge("handle_error", END)

    graph = builder.compile(checkpointer=checkpointer)

    return graph
