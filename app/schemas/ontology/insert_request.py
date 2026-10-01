"""API request/response schemas for the ontology-insert POC's HTTP surface
(`app/api/v1/ontology_insert.py`). Sibling to
`app.schemas.ontology.update_request`, with one addition: the decision
endpoint accepts either an approve/reject `decision` (same as UPDATE) or a
`details` payload answering a `missing_required_fields` interrupt -- this
graph has two interrupt points, not one (see
`app/agents/ontology_insert/graph.py`).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel

from app.schemas.ontology.insert_proposal import InsertOperationProposal


class StartOntologyInsertRequest(BaseModel):
    message: str


class OntologyInsertDecisionRequest(BaseModel):
    """Exactly one of `decision` (resumes a pending approval) or `details`
    (resumes a pending missing-fields request) must be supplied -- which
    one is valid depends on which interrupt is currently paused for the
    given thread_id. Enforced in the route handler
    (`app/api/v1/ontology_insert.py`), not a pydantic validator: a
    validator raising `ValueError` here puts a non-JSON-serializable
    exception into `RequestValidationError.errors()`'s `ctx`, which
    `app.core.exceptions`'s 422 handler doesn't sanitize -- a plain field
    check in the route avoids that pre-existing trap entirely."""

    decision: Literal["approve", "reject"] | None = None
    details: dict[str, str] | None = None


class OntologyInsertStatus(str, Enum):
    """Every outcome the graph can legitimately end a call in -- all data,
    not exceptions. An unknown `thread_id` is the one case that *is* a
    genuine HTTP error (404), not a status here -- see
    `OntologyInsertRunService.submit_decision`."""

    AWAITING_DETAILS = "awaiting_details"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class OntologyInsertResponse(BaseModel):
    thread_id: str
    status: OntologyInsertStatus
    proposal: InsertOperationProposal | None = None
    missing_fields: list[str] | None = None
    execution_result: dict[str, str] | None = None
    error: dict[str, str] | None = None
