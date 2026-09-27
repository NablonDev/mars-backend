"""State for the Ontology-Context-Driven Update POC graph
(`app/agents/ontology_update/`) -- proves a LangGraph HITL agent can use
`OntologyContextService` (Phase 1) to understand an existing entity and
safely update one existing row after human approval.

Plain `TypedDict(total=False)`, matching `app.agents.cmir.state.GraphState`/
`app.agents.po_validation.state.POGraphState`'s convention exactly: fully
JSON-serializable (no DB session, no ORM row, no `rdflib.Graph`, no
repository/service object), since LangGraph's checkpointer persists this
between the interrupt and the resume.
"""

from __future__ import annotations

from typing import Any, TypedDict


class OntologyUpdateState(TypedDict, total=False):
    # Set by interpret_request.
    user_request: str
    operation: str
    source_material_code: str
    replacement_material_code: str

    # Set by load_semantic_context -- discovered from
    # OntologyContextService.get_relationships(...), never hardcoded. See
    # nodes.py's module docstring.
    semantic_context: dict[str, Any]

    # Set by resolve_target.
    resolved_material: dict[str, Any]
    resolved_material_master: dict[str, Any]
    clarification_required: bool
    clarification_reason: str

    # Set by resolve_replacement.
    resolved_replacement_material: dict[str, Any]

    # Set by build_proposal.
    proposal: dict[str, Any]

    # Set by human_approval (the interrupt node) and, on resume, read back
    # from Command(resume=...). "approve" | "reject" -- str rather than a
    # Literal, matching app.agents.cmir.state.GraphState's own
    # decision: str | None convention.
    approval_status: str | None

    # Set by execute_update (Checkpoint 3 -- not implemented yet).
    execution_result: dict[str, Any]

    # Set by any node that fails instead of raising -- mirrors
    # POGraphState's "error" field/`_capture_errors` convention.
    error: dict[str, Any]
