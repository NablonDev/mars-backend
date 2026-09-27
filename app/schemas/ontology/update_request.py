"""API request/response schemas for the ontology-update POC's HTTP surface
(`app/api/v1/ontology_update.py`). See
`docs/ontology/semantic-context-layer-design.md` §23 for the flow this
implements.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel

from app.schemas.ontology.update_proposal import OperationProposal


class StartOntologyUpdateRequest(BaseModel):
    message: str


class OntologyUpdateDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]


class OntologyUpdateStatus(str, Enum):
    """Every outcome the graph can legitimately end a call in -- all data,
    not exceptions, mirroring how the graph itself represents them in
    state (see `app/agents/ontology_update/nodes.py`). An unknown
    `thread_id` is the one case that *is* a genuine HTTP error (404), not
    a status here -- see `OntologyUpdateRunService.submit_decision`."""

    AWAITING_APPROVAL = "awaiting_approval"
    CLARIFICATION_REQUIRED = "clarification_required"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class OntologyUpdateResponse(BaseModel):
    thread_id: str
    status: OntologyUpdateStatus
    proposal: OperationProposal | None = None
    execution_result: dict[str, str | None] | None = None
    clarification_reason: str | None = None
    error: dict[str, str] | None = None
