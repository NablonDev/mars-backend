"""Wires the ontology-insert POC's node functions into a graph.

interpret_request -> load_semantic_context -> check_required_fields ->
{missing: request_missing_details -> check_required_fields (loop),
 continue: check_for_conflicts} -> build_proposal -> human_approval ->
{rejected: END, approved: execute_insert -> END}.

Two interrupt nodes on one graph, unlike the UPDATE POC's one:
`request_missing_details` can fire zero or more times (looping back into
`check_required_fields` after each resume) before the graph ever reaches
`build_proposal`/`human_approval` -- see `nodes.py`'s module docstring for
why this exists (the request sentence's three fields are each optional,
and a human is asked for whichever ones are missing rather than the
request being rejected outright).

Every non-continue route that isn't a loop-back ends the graph immediately
via END, with the relevant detail left in state for the caller to read --
no separate error-handling node is needed at this scope, since nothing has
to be cleaned up (no DB write has happened by this point).

`execute_insert` is the only node that writes anything, and it is only
reachable via the "approved" branch of `human_approval`'s conditional edge.
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from app.agents.ontology_insert.nodes import OntologyInsertNodes
from app.agents.ontology_insert.state import OntologyInsertState
from app.core.tracing import traced
from app.repositories.process.agent_registry import AgentTraceRepository


def build_ontology_insert_graph(
    nodes: OntologyInsertNodes,
    checkpointer: BaseCheckpointSaver,
    trace_repo: AgentTraceRepository,
):
    graph = StateGraph(OntologyInsertState)

    def node(name: str, fn):
        return traced(name, fn, trace_repo)

    graph.add_node("interpret_request", node("interpret_request", nodes.interpret_request))
    graph.add_node("load_semantic_context", node("load_semantic_context", nodes.load_semantic_context))
    graph.add_node("check_required_fields", node("check_required_fields", nodes.check_required_fields))
    graph.add_node("request_missing_details", node("request_missing_details", nodes.request_missing_details))
    graph.add_node("check_for_conflicts", node("check_for_conflicts", nodes.check_for_conflicts))
    graph.add_node("build_proposal", node("build_proposal", nodes.build_proposal))
    graph.add_node("human_approval", node("human_approval", nodes.human_approval))
    graph.add_node("execute_insert", node("execute_insert", nodes.execute_insert))

    graph.set_entry_point("interpret_request")

    graph.add_conditional_edges(
        "interpret_request",
        nodes.route_after_interpret,
        {"error": END, "continue": "load_semantic_context"},
    )
    graph.add_conditional_edges(
        "load_semantic_context",
        nodes.route_after_context,
        {"error": END, "continue": "check_required_fields"},
    )
    graph.add_conditional_edges(
        "check_required_fields",
        nodes.route_after_required_fields_check,
        {"missing": "request_missing_details", "continue": "check_for_conflicts"},
    )
    graph.add_edge("request_missing_details", "check_required_fields")
    graph.add_conditional_edges(
        "check_for_conflicts",
        nodes.route_after_conflict_check,
        {"error": END, "continue": "build_proposal"},
    )
    graph.add_edge("build_proposal", "human_approval")
    graph.add_conditional_edges(
        "human_approval",
        nodes.route_after_approval,
        {"rejected": END, "approved": "execute_insert"},
    )
    graph.add_edge("execute_insert", END)

    return graph.compile(checkpointer=checkpointer)
