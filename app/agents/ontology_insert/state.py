"""State for the Ontology-Context-Driven Insert POC graph
(`app/agents/ontology_insert/`) -- sibling to `app.agents.ontology_update`,
proving the same Agent -> Service -> Repository -> PostgreSQL boundary and
ontology-context-driven field discovery generalizes from an UPDATE to a
CREATE: one request creates a new `Material`, a `MaterialMaster` row for
it, and the `Plant` it's located at (reused if the plant code already
exists) -- all after human approval.

Plain `TypedDict(total=False)`, matching `OntologyUpdateState`'s convention
exactly: fully JSON-serializable, since LangGraph's checkpointer persists
this across every interrupt/resume round trip (there are two interrupt
points in this graph -- see `nodes.py`'s module docstring -- unlike
UPDATE's one).
"""

from __future__ import annotations

from typing import Any, TypedDict


class OntologyInsertState(TypedDict, total=False):
    # Set by interpret_request. Each of the three identifier fields is
    # optional at this point -- interpret_request extracts whichever the
    # sentence actually supplied, leaving the key absent (not set to None
    # -- `total=False` already models "not known yet") for anything
    # missing. Missing ones are filled in later via the missing-details
    # HITL round (see check_required_fields/request_missing_details
    # below), not re-parsed from free text.
    user_request: str
    operation: str
    material_code: str
    plant_code: str
    sap_material_number: str

    # Set by load_semantic_context -- discovered from
    # OntologyContextService.get_entity_context(...)/get_relationships(...),
    # never hardcoded. See nodes.py's module docstring.
    semantic_context: dict[str, Any]

    # Set by check_required_fields. Non-empty iff request_missing_details
    # is about to run; re-checked after every resume from that node, so it
    # reflects "still missing right now", not "was missing once".
    missing_fields: list[str]

    # Set by build_proposal.
    proposal: dict[str, Any]

    # Set by human_approval (the approval interrupt node) and, on resume,
    # read back from Command(resume=...). "approve" | "reject".
    approval_status: str | None

    # Set by execute_insert.
    execution_result: dict[str, Any]

    # Set by any node that fails instead of raising -- mirrors
    # OntologyUpdateState's "error" field convention.
    error: dict[str, Any]
