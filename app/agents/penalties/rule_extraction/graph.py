"""Build the LangGraph workflow for penalty rule extraction.

The graph screens contract sections, resolves candidate clauses, extracts penalty
rules and facts, and stages the results for review.
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.agents.penalties.rule_extraction.nodes import RuleExtractionNodes
from app.agents.penalties.rule_extraction.state import RuleExtractionState
from app.core.tracing import traced
from app.repositories.process.agent_registry import AgentTraceRepository


def build_graph(
    nodes: RuleExtractionNodes,
    checkpointer: BaseCheckpointSaver,
    trace_repo: AgentTraceRepository,
):
    """Build the penalty rule extraction workflow graph."""
    builder = StateGraph(RuleExtractionState)

    def node(name: str, fn):
        """Wrap a WorkflowNodes method with tracing so its invocation is recorded to process.agent_trace."""
        return traced(name, fn, trace_repo)

    builder.add_node("split_document", node("split_document", nodes.split_document))
    builder.add_node("screen_unit", node("screen_unit", nodes.screen_unit))
    builder.add_node("resolve_candidates", node("resolve_candidates", nodes.resolve_candidates))
    builder.add_node("process_clause", node("process_clause", nodes.process_clause))
    builder.add_node("stage_rules", node("stage_rules", nodes.stage_rules))

    builder.add_edge(START, "split_document")
    builder.add_conditional_edges("split_document", nodes.route_after_split_document)
    builder.add_edge("screen_unit", "resolve_candidates")
    builder.add_conditional_edges("resolve_candidates", nodes.route_after_resolve_candidates)
    builder.add_edge("process_clause", "stage_rules")
    builder.add_edge("stage_rules", END)

    graph = builder.compile(checkpointer=checkpointer)

    return graph
