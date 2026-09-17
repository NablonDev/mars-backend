"""Penalty rule extraction workflow graph builder.

Orchestrates segmentation, per-unit screening, excerpt resolution, per-clause
classification and fact extraction, staged persistence, human review, and decision
application for one contract. Two `Send` fan-outs (`screen_unit`, `process_clause`)
join back into one fan-in node each (`resolve_candidates`, `stage_rules`); see the
module docstring in `nodes.py` for why those two are the only nodes allowed to write to
Postgres. `human_review` and `apply_decisions` stay separate nodes: LangGraph re-runs an
interrupted node from its start on resume, and a combined node would re-persist every
staged row on every resume.
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

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
    builder.add_node("human_review", node("human_review", nodes.human_review))
    builder.add_node("apply_decisions", node("apply_decisions", nodes.apply_decisions))

    builder.set_entry_point("split_document")
    builder.add_conditional_edges("split_document", nodes.route_after_split_document)
    builder.add_edge("screen_unit", "resolve_candidates")
    builder.add_conditional_edges("resolve_candidates", nodes.route_after_resolve_candidates)
    builder.add_edge("process_clause", "stage_rules")
    builder.add_edge("stage_rules", "human_review")
    builder.add_edge("human_review", "apply_decisions")
    builder.add_edge("apply_decisions", END)
    graph = builder.compile(checkpointer=checkpointer)

    return graph
