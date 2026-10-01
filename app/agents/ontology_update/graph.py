"""Wires the ontology-update POC's node functions into a graph.

interpret_request -> load_semantic_context -> resolve_target ->
resolve_replacement -> build_proposal -> human_approval ->
{rejected: END, approved: execute_update -> END}.

Every non-continue route (interpret/context error, resolve error,
ambiguous MaterialMaster) ends the graph immediately via END, with the
relevant detail left in state for the caller to read -- no separate
error-handling node is needed at this scope, since nothing has to be
cleaned up (no DB write has happened by this point).

`execute_update` (Checkpoint 3) is the only node that writes anything, and
it is only reachable via the "approved" branch of `human_approval`'s
conditional edge -- the "rejected" branch goes straight to END, so a
rejected proposal can never reach it. See `nodes.py`'s module docstring
for the Agent -> Service -> Repository boundary `execute_update` itself
enforces.
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from app.agents.ontology_update.nodes import OntologyUpdateNodes
from app.agents.ontology_update.state import OntologyUpdateState
from app.core.tracing import traced
from app.repositories.process.agent_registry import AgentTraceRepository


def build_ontology_update_graph(
    nodes: OntologyUpdateNodes,
    checkpointer: BaseCheckpointSaver,
    trace_repo: AgentTraceRepository,
):
    graph = StateGraph(OntologyUpdateState)

    def node(name: str, fn):
        return traced(name, fn, trace_repo)

    graph.add_node("interpret_request", node("interpret_request", nodes.interpret_request))
    graph.add_node("load_semantic_context", node("load_semantic_context", nodes.load_semantic_context))
    graph.add_node("resolve_target", node("resolve_target", nodes.resolve_target))
    graph.add_node("resolve_replacement", node("resolve_replacement", nodes.resolve_replacement))
    graph.add_node("build_proposal", node("build_proposal", nodes.build_proposal))
    graph.add_node("human_approval", node("human_approval", nodes.human_approval))
    graph.add_node("execute_update", node("execute_update", nodes.execute_update))

    graph.set_entry_point("interpret_request")

    graph.add_conditional_edges(
        "interpret_request",
        nodes.route_after_interpret,
        {"error": END, "continue": "load_semantic_context"},
    )
    graph.add_conditional_edges(
        "load_semantic_context",
        nodes.route_after_context,
        {"error": END, "continue": "resolve_target"},
    )
    graph.add_conditional_edges(
        "resolve_target",
        nodes.route_after_resolve_target,
        {"error": END, "clarification_required": END, "continue": "resolve_replacement"},
    )
    graph.add_conditional_edges(
        "resolve_replacement",
        nodes.route_after_resolve_replacement,
        {"error": END, "continue": "build_proposal"},
    )
    graph.add_edge("build_proposal", "human_approval")
    graph.add_conditional_edges(
        "human_approval",
        nodes.route_after_approval,
        {"rejected": END, "approved": "execute_update"},
    )
    graph.add_edge("execute_update", END)

    return graph.compile(checkpointer=checkpointer)
